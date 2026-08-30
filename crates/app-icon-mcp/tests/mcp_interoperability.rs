//! Wire-level interoperability tests for the MCP adapter and stdio binary.

use std::error::Error;
use std::fs;
use std::io;
use std::path::Path;
use std::process::Stdio;
use std::time::Duration;

use app_icon_mcp::IconMcpServer;
use image::{ImageBuffer, Rgba};
use rmcp::{
    ServiceExt,
    model::{CallToolRequestParams, Tool},
};
use serde_json::{Map, Value, json};
use tempfile::tempdir;
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    process::{ChildStdout, Command},
    time::timeout,
};

type TestResult<T = ()> = Result<T, Box<dyn Error + Send + Sync>>;

#[test]
fn plugin_manifest_contract_matches_the_cargo_binary() -> TestResult {
    let workspace = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let plugin: Value =
        serde_json::from_slice(&fs::read(workspace.join(".codex-plugin/plugin.json"))?)?;
    let mcp: Value = serde_json::from_slice(&fs::read(workspace.join(".mcp.json"))?)?;

    assert_eq!(
        plugin.get("name").and_then(Value::as_str),
        Some("app-icon-toolkit")
    );
    assert_eq!(
        plugin.get("version").and_then(Value::as_str),
        Some(env!("CARGO_PKG_VERSION"))
    );
    assert_eq!(
        plugin.get("mcpServers").and_then(Value::as_str),
        Some("./.mcp.json")
    );

    let launcher = mcp
        .pointer("/mcpServers/app-icon-toolkit")
        .and_then(Value::as_object)
        .ok_or_else(|| io::Error::other(".mcp.json omitted app-icon-toolkit launcher"))?;
    let command = launcher
        .get("command")
        .and_then(Value::as_str)
        .ok_or_else(|| io::Error::other("MCP launcher omitted string command"))?;
    assert_eq!(command, "./bin/app-icon-toolkit-mcp");
    assert_eq!(launcher.get("cwd").and_then(Value::as_str), Some("."));
    assert!(
        launcher
            .get("args")
            .and_then(Value::as_array)
            .is_some_and(Vec::is_empty)
    );

    let cargo_binary = Path::new(env!("CARGO_BIN_EXE_app-icon-toolkit-mcp"))
        .file_stem()
        .and_then(|name| name.to_str())
        .ok_or_else(|| io::Error::other("Cargo binary name was not portable UTF-8"))?;
    let launcher_binary = Path::new(command)
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| io::Error::other("launcher command omitted a portable binary name"))?;
    assert_eq!(launcher_binary, cargo_binary);
    Ok(())
}

#[tokio::test]
async fn duplex_client_lists_strict_schemas_and_receives_structured_errors() -> TestResult {
    let (server_transport, client_transport) = tokio::io::duplex(64 * 1_024);
    let server_task = tokio::spawn(async move {
        IconMcpServer::default()
            .serve(server_transport)
            .await?
            .waiting()
            .await?;
        TestResult::Ok(())
    });
    let client = ().serve(client_transport).await?;

    let mut tools = client.list_all_tools().await?;
    tools.sort_by(|left, right| left.name.cmp(&right.name));
    assert_eq!(
        tools
            .iter()
            .map(|tool| tool.name.as_ref())
            .collect::<Vec<_>>(),
        vec!["generate_icon_set", "plan_icon_set"]
    );

    let generate = find_tool(&tools, "generate_icon_set")?;
    let plan = find_tool(&tools, "plan_icon_set")?;
    assert_request_schema(generate)?;
    assert_request_schema(plan)?;
    assert_output_schema(generate, &["output_directory", "artifacts"])?;
    assert_output_schema(plan, &["output_directory", "sources", "profiles"])?;
    assert_annotations(generate, false, false, false, false)?;
    assert_annotations(plan, true, false, true, false)?;

    let workspace = tempdir()?;
    create_test_sources(workspace.path())?;
    let msix_arguments = object(json!({
        "workspace_root": workspace.path().canonicalize()?,
        "output_directory": "generated",
        "sources": { "flattened": "sources/flattened.png" },
        "targets": [{ "profile": "windows_msix_assets" }]
    }))?;
    let msix_plan = client
        .call_tool(CallToolRequestParams::new("plan_icon_set").with_arguments(msix_arguments))
        .await?;
    assert_ne!(msix_plan.is_error, Some(true));
    let structured_msix_plan = msix_plan
        .structured_content
        .as_ref()
        .ok_or_else(|| io::Error::other("MSIX plan omitted structured JSON content"))?;
    assert_eq!(
        structured_msix_plan
            .pointer("/profiles/0/profile")
            .and_then(Value::as_str),
        Some("windows_msix_assets")
    );
    let msix_artifacts = structured_msix_plan
        .pointer("/profiles/0/artifacts")
        .and_then(Value::as_array)
        .ok_or_else(|| io::Error::other("MSIX plan omitted its artifact matrix"))?;
    assert_eq!(msix_artifacts.len(), 57);
    assert_eq!(
        msix_artifacts[0].get("path").and_then(Value::as_str),
        Some("windows/msix/Assets/AppList.targetsize-16.png")
    );
    assert_eq!(
        msix_artifacts[56].get("path").and_then(Value::as_str),
        Some("windows/msix/Assets/StoreLogo.scale-400.png")
    );

    let arguments = object(json!({
        "workspace_root": "relative/workspace",
        "output_directory": "generated",
        "sources": { "flattened": "sources/flattened.png" },
        "targets": [{
            "profile": "windows_ico",
            "file_stem": "icon-probe"
        }]
    }))?;
    let failure = client
        .call_tool(CallToolRequestParams::new("plan_icon_set").with_arguments(arguments))
        .await?;
    assert_eq!(failure.is_error, Some(true));
    let structured = failure
        .structured_content
        .as_ref()
        .and_then(Value::as_object)
        .ok_or_else(|| io::Error::other("tool failure omitted structured JSON content"))?;
    assert_eq!(
        structured.get("code").and_then(Value::as_str),
        Some("INVALID_REQUEST")
    );
    assert!(
        structured
            .get("message")
            .and_then(Value::as_str)
            .is_some_and(|message| message.contains("workspace root must be an absolute path"))
    );

    client.cancel().await?;
    server_task.await??;
    Ok(())
}

#[tokio::test]
async fn concurrent_generate_calls_have_one_success_and_one_busy_failure() -> TestResult {
    let workspace = tempdir()?;
    create_test_sources(workspace.path())?;
    let (server_transport, client_transport) = tokio::io::duplex(128 * 1_024);
    let server_task = tokio::spawn(async move {
        IconMcpServer::default()
            .serve(server_transport)
            .await?
            .waiting()
            .await?;
        TestResult::Ok(())
    });
    let client = ().serve(client_transport).await?;

    let first = client.call_tool(
        CallToolRequestParams::new("generate_icon_set")
            .with_arguments(generation_arguments(workspace.path(), "first")?),
    );
    let second = client.call_tool(
        CallToolRequestParams::new("generate_icon_set")
            .with_arguments(generation_arguments(workspace.path(), "second")?),
    );
    let (first, second) = tokio::join!(first, second);
    let responses = [first?, second?];

    assert_eq!(
        responses
            .iter()
            .filter(|response| response.is_error != Some(true))
            .count(),
        1
    );
    let failure = responses
        .iter()
        .find(|response| response.is_error == Some(true))
        .and_then(|response| response.structured_content.as_ref())
        .and_then(Value::as_object)
        .ok_or_else(|| io::Error::other("busy response omitted structured failure"))?;
    assert_eq!(failure.get("code").and_then(Value::as_str), Some("BUSY"));
    assert_ne!(
        workspace.path().join("first").exists(),
        workspace.path().join("second").exists()
    );

    client.cancel().await?;
    server_task.await??;
    Ok(())
}

#[tokio::test]
async fn stdio_binary_speaks_only_newline_delimited_json_rpc_on_stdout() -> TestResult {
    let mut child = Command::new(env!("CARGO_BIN_EXE_app-icon-toolkit-mcp"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()?;
    let mut stdin = child
        .stdin
        .take()
        .ok_or_else(|| io::Error::other("child process did not expose stdin"))?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| io::Error::other("child process did not expose stdout"))?;
    let mut stdout = BufReader::new(stdout);
    let mut seen_messages = Vec::new();

    write_message(
        &mut stdin,
        &json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": { "name": "app-icon-toolkit-test", "version": "1.0.0" }
            }
        }),
    )
    .await?;
    let initialize = read_response(&mut stdout, 1, &mut seen_messages).await?;
    assert_eq!(
        initialize
            .pointer("/result/serverInfo/name")
            .and_then(Value::as_str),
        Some("app-icon-toolkit")
    );
    assert_eq!(
        initialize
            .pointer("/result/protocolVersion")
            .and_then(Value::as_str),
        Some("2025-11-25")
    );

    write_message(
        &mut stdin,
        &json!({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {}
        }),
    )
    .await?;
    write_message(
        &mut stdin,
        &json!({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {}
        }),
    )
    .await?;
    let listed = read_response(&mut stdout, 2, &mut seen_messages).await?;
    let tools = listed
        .pointer("/result/tools")
        .and_then(Value::as_array)
        .ok_or_else(|| io::Error::other("stdio tools/list omitted its tools array"))?;
    assert_eq!(tools.len(), 2);
    assert!(tools.iter().all(|tool| tool.get("inputSchema").is_some()));

    stdin.shutdown().await?;
    drop(stdin);
    let mut trailing_stdout = String::new();
    timeout(
        Duration::from_secs(5),
        stdout.read_to_string(&mut trailing_stdout),
    )
    .await??;
    for line in trailing_stdout.lines().filter(|line| !line.is_empty()) {
        seen_messages.push(serde_json::from_str::<Value>(line)?);
    }
    assert!(
        seen_messages
            .iter()
            .all(|message| message.get("jsonrpc").and_then(Value::as_str) == Some("2.0"))
    );

    let status = timeout(Duration::from_secs(5), child.wait()).await??;
    assert!(status.success(), "stdio server exited with {status}");
    let mut stderr_text = String::new();
    if let Some(mut stderr) = child.stderr.take() {
        stderr.read_to_string(&mut stderr_text).await?;
    }
    assert!(
        !stderr_text.contains('\u{1b}'),
        "stderr contained ANSI escapes"
    );
    Ok(())
}

fn find_tool<'a>(tools: &'a [Tool], name: &str) -> TestResult<&'a Tool> {
    tools
        .iter()
        .find(|tool| tool.name == name)
        .ok_or_else(|| io::Error::other(format!("tool `{name}` was not listed")).into())
}

fn assert_request_schema(tool: &Tool) -> TestResult {
    let schema = tool.input_schema.as_ref();
    assert_eq!(schema.get("type").and_then(Value::as_str), Some("object"));
    assert_eq!(
        schema.get("additionalProperties").and_then(Value::as_bool),
        Some(false)
    );
    assert_required(
        schema,
        &["workspace_root", "output_directory", "sources", "targets"],
    )?;

    let schema_text = serde_json::to_string(schema)?;
    for discriminator in [
        "mac_os_app_icon_set",
        "android_adaptive",
        "windows_ico",
        "windows_msix_assets",
        "linux_xdg",
    ] {
        assert!(
            schema_text.contains(discriminator),
            "input schema omitted target discriminator `{discriminator}`"
        );
    }
    assert!(
        tool.description
            .as_deref()
            .is_some_and(|description| !description.is_empty())
    );
    Ok(())
}

fn assert_output_schema(tool: &Tool, required: &[&str]) -> TestResult {
    let schema = tool
        .output_schema
        .as_deref()
        .ok_or_else(|| io::Error::other(format!("tool `{}` omitted outputSchema", tool.name)))?;
    assert_eq!(schema.get("type").and_then(Value::as_str), Some("object"));
    assert_required(schema, required)
}

fn create_test_sources(root: &Path) -> TestResult {
    let sources = root.join("sources");
    fs::create_dir(&sources)?;
    let image = ImageBuffer::from_pixel(1_024, 1_024, Rgba([37_u8, 109, 219, 255]));
    for name in ["flattened", "foreground", "background", "monochrome"] {
        image.save(sources.join(format!("{name}.png")))?;
    }
    Ok(())
}

fn generation_arguments(root: &Path, output_directory: &str) -> TestResult<Map<String, Value>> {
    object(json!({
        "workspace_root": root.canonicalize()?,
        "output_directory": output_directory,
        "sources": {
            "flattened": "sources/flattened.png",
            "adaptive": {
                "foreground": "sources/foreground.png",
                "background": "sources/background.png",
                "monochrome": "sources/monochrome.png"
            }
        },
        "targets": [
            { "profile": "mac_os_app_icon_set", "icon_set_name": "Assets" },
            { "profile": "android_adaptive", "resource_name": "ic_launcher" },
            { "profile": "windows_ico", "file_stem": "app-icon" },
            {
                "profile": "linux_xdg",
                "application_id": "com.example.IconProbe",
                "display_name": "Icon Probe",
                "executable": "icon-probe"
            }
        ]
    }))
}

fn assert_required(schema: &Map<String, Value>, expected: &[&str]) -> TestResult {
    let required = schema
        .get("required")
        .and_then(Value::as_array)
        .ok_or_else(|| io::Error::other("JSON Schema omitted its required array"))?;
    for field in expected {
        assert!(
            required.iter().any(|value| value.as_str() == Some(field)),
            "JSON Schema omitted required field `{field}`"
        );
    }
    Ok(())
}

fn assert_annotations(
    tool: &Tool,
    read_only: bool,
    destructive: bool,
    idempotent: bool,
    open_world: bool,
) -> TestResult {
    let annotations = tool
        .annotations
        .as_ref()
        .ok_or_else(|| io::Error::other(format!("tool `{}` omitted annotations", tool.name)))?;
    assert_eq!(annotations.read_only_hint, Some(read_only));
    assert_eq!(annotations.destructive_hint, Some(destructive));
    assert_eq!(annotations.idempotent_hint, Some(idempotent));
    assert_eq!(annotations.open_world_hint, Some(open_world));
    assert!(
        annotations
            .title
            .as_deref()
            .is_some_and(|title| !title.is_empty())
    );
    Ok(())
}

fn object(value: Value) -> TestResult<Map<String, Value>> {
    value
        .as_object()
        .cloned()
        .ok_or_else(|| io::Error::other("test fixture was not a JSON object").into())
}

async fn write_message(stdin: &mut tokio::process::ChildStdin, message: &Value) -> TestResult {
    let mut encoded = serde_json::to_vec(message)?;
    encoded.push(b'\n');
    stdin.write_all(&encoded).await?;
    stdin.flush().await?;
    Ok(())
}

async fn read_response(
    stdout: &mut BufReader<ChildStdout>,
    expected_id: u64,
    seen_messages: &mut Vec<Value>,
) -> TestResult<Value> {
    for _ in 0..16 {
        let mut line = String::new();
        let bytes = timeout(Duration::from_secs(5), stdout.read_line(&mut line)).await??;
        if bytes == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                format!("stdio server closed before response id {expected_id}"),
            )
            .into());
        }
        let message: Value = serde_json::from_str(line.trim_end())?;
        seen_messages.push(message.clone());
        if message.get("id").and_then(Value::as_u64) == Some(expected_id) {
            return Ok(message);
        }
    }
    Err(io::Error::other(format!(
        "stdio server did not return response id {expected_id}"
    ))
    .into())
}

//! MCP transport adapter for App Icon Toolkit.
//!
//! This crate is the only workspace layer that depends on `rmcp`. Request
//! values are validated and converted to domain objects before the engine is
//! invoked, and domain or engine types never carry protocol concerns.

mod schema;

use std::{error::Error, sync::Arc};

use app_icon_engine::{EngineError, IconService};
use rmcp::{
    Json, ServerHandler, ServiceExt,
    handler::server::{router::tool::ToolRouter, wrapper::Parameters},
    tool, tool_handler, tool_router,
    transport::stdio,
};
use schema::{
    GenerateIconSetResponse, IconSetRequest, PlanIconSetResponse, ToolFailure, ToolInvocation,
};
use tokio::{
    sync::{OwnedSemaphorePermit, Semaphore, TryAcquireError},
    task::{JoinError, spawn_blocking},
};

const MAX_CONCURRENT_PLANS: usize = 4;

/// MCP server exposing read-only planning and create-new icon generation.
#[derive(Clone)]
pub struct IconMcpServer {
    service: Arc<IconService>,
    plan_permit: Arc<Semaphore>,
    generate_permit: Arc<Semaphore>,
    tool_router: ToolRouter<Self>,
}

impl IconMcpServer {
    /// Creates a server around an icon service.
    #[must_use]
    pub fn new(service: IconService) -> Self {
        Self {
            service: Arc::new(service),
            plan_permit: Arc::new(Semaphore::new(MAX_CONCURRENT_PLANS)),
            generate_permit: Arc::new(Semaphore::new(1)),
            tool_router: Self::tool_router(),
        }
    }

    fn engine_failure(error: &EngineError) -> ToolFailure {
        ToolFailure::engine(error.code(), error.message(), error.relative_path())
    }

    fn worker_failure(operation: &'static str, error: &JoinError) -> ToolFailure {
        tracing::error!(operation, %error, "blocking icon worker failed");
        ToolFailure::internal(format!("{operation} worker stopped unexpectedly"))
    }

    fn try_permit(
        semaphore: &Arc<Semaphore>,
        operation: &'static str,
    ) -> Result<OwnedSemaphorePermit, ToolFailure> {
        match Arc::clone(semaphore).try_acquire_owned() {
            Ok(permit) => Ok(permit),
            Err(TryAcquireError::NoPermits) => Err(ToolFailure::busy(operation)),
            Err(TryAcquireError::Closed) => Err(ToolFailure::internal(format!(
                "{operation} concurrency controller is unavailable"
            ))),
        }
    }
}

impl Default for IconMcpServer {
    fn default() -> Self {
        Self::new(IconService::new())
    }
}

#[tool_router(router = tool_router)]
impl IconMcpServer {
    /// Validate PNG sources and return the exact artifact plan without writing files.
    #[tool(
        name = "plan_icon_set",
        annotations(
            title = "Plan application icon set",
            read_only_hint = true,
            destructive_hint = false,
            idempotent_hint = true,
            open_world_hint = false
        )
    )]
    async fn plan_icon_set(
        &self,
        Parameters(request): Parameters<IconSetRequest>,
    ) -> Result<Json<PlanIconSetResponse>, Json<ToolFailure>> {
        let invocation = ToolInvocation::try_from(request).map_err(Json)?;
        let permit = Self::try_permit(&self.plan_permit, "planning").map_err(Json)?;
        let service = Arc::clone(&self.service);

        let result = spawn_blocking(move || {
            let _permit = permit;
            service.plan(&invocation.workspace_root, &invocation.job)
        })
        .await
        .map_err(|error| Json(Self::worker_failure("plan", &error)))?;

        result
            .map(|plan| Json(PlanIconSetResponse::from(&plan)))
            .map_err(|error| Json(Self::engine_failure(&error)))
    }

    /// Create a complete icon set in a new output directory.
    #[tool(
        name = "generate_icon_set",
        annotations(
            title = "Generate application icon set",
            read_only_hint = false,
            destructive_hint = false,
            idempotent_hint = false,
            open_world_hint = false
        )
    )]
    async fn generate_icon_set(
        &self,
        Parameters(request): Parameters<IconSetRequest>,
    ) -> Result<Json<GenerateIconSetResponse>, Json<ToolFailure>> {
        let invocation = ToolInvocation::try_from(request).map_err(Json)?;
        let permit = Self::try_permit(&self.generate_permit, "generation").map_err(Json)?;
        let service = Arc::clone(&self.service);

        let result = spawn_blocking(move || {
            let _permit = permit;
            service.generate(&invocation.workspace_root, &invocation.job)
        })
        .await
        .map_err(|error| Json(Self::worker_failure("generate", &error)))?;

        result
            .map(|report| {
                Json(GenerateIconSetResponse::new(
                    report.output_directory(),
                    report.artifacts(),
                ))
            })
            .map_err(|error| Json(Self::engine_failure(&error)))
    }
}

#[tool_handler(
    router = self.tool_router,
    name = "app-icon-toolkit",
    instructions = "Plan or create deterministic application icon assets from explicit PNG sources. Paths inside a job are relative to the absolute workspace_root, and generation never overwrites an existing output directory."
)]
impl ServerHandler for IconMcpServer {}

/// Runs the App Icon Toolkit MCP server over stdin and stdout.
///
/// Diagnostics are written exclusively to stderr without ANSI escape codes.
pub async fn run_stdio() -> Result<(), Box<dyn Error + Send + Sync>> {
    tracing_subscriber::fmt()
        .with_ansi(false)
        .with_writer(std::io::stderr)
        .try_init()?;

    let service = IconMcpServer::default().serve(stdio()).await?;
    service.waiting().await?;
    Ok(())
}

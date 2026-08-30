//! App Icon Toolkit MCP server entry point.

use std::error::Error;

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error + Send + Sync>> {
    app_icon_mcp::run_stdio().await
}

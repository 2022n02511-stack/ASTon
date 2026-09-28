import asyncio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

async def main():
    # Configure the Codemod MCP server process
    server_params = StdioServerParameters(
        command="npx",
        args=["codemod@latest", "mcp"]
    )

    # Spawn the process over stdio and open the session
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            
            # Query the tools offered by the Codemod MCP server
            tools = await session.list_tools()
            print("\n--- Exposed Codemod MCP Tools ---")
            for t in tools.tools:
                print(f"Tool: {t.name}\nDescription: {t.description[:100]}...\n")

if __name__ == "__main__":
    asyncio.run(main())
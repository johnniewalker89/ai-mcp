from mcp_greenplum.mcp_env import TransportType, get_mcp_config
from mcp_greenplum.mcp_server import mcp


def main() -> None:
    mcp_config = get_mcp_config()
    transport = mcp_config.server_transport

    http_transports = [TransportType.HTTP.value, TransportType.SSE.value]
    if transport in http_transports:
        mcp.run(transport=transport, host=mcp_config.bind_host, port=mcp_config.bind_port)
    else:
        mcp.run(transport=transport)


if __name__ == "__main__":
    main()


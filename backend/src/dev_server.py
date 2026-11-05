import socket

import uvicorn


config = uvicorn.Config("src.server:app", host="127.0.0.1", port=8765)
with socket.create_server((config.host, config.port)) as listener:
    server = uvicorn.Server(config)
    server.run(sockets=[listener])
    if not server.started:
        raise SystemExit(1)

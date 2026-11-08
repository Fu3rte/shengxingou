import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest


@pytest.fixture
def model_server():
    # 回放 OpenAI 协议响应，仅验证集成链路。
    requests, responses = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            response = responses.pop(0)
            if body['stream']:
                chunks = []
                for delta, reason in response:
                    if delta is None:
                        chunks.append({'id': 'chat-test', 'object': 'chat.completion.chunk',
                                       'created': 1, 'model': 'fixture', 'choices': [], 'usage': reason})
                    else:
                        chunks.append({'id': 'chat-test', 'object': 'chat.completion.chunk',
                                       'created': 1, 'model': 'fixture', 'choices': [
                                           {'index': 0, 'delta': delta, 'finish_reason': reason}]})
                data = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n'
                content_type = 'text/event-stream'
            else:
                data = json.dumps(response)
                content_type = 'application/json'
            encoded = data.encode()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', requests, responses
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

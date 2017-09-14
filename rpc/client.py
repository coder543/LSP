import json
import sys
import threading
from .protocol import Request, Notification
from ..core.logging import debug, printf, server_log
from ..core.events import Events
from ..core.settings import log_stderr


def format_request(payload: 'Dict[str, Any]'):
    """Converts the request into json and adds the Content-Length header"""
    content = json.dumps(payload, sort_keys=False)
    content_length = len(content)
    result = "Content-Length: {}\r\n\r\n{}".format(content_length, content)
    return result


class Client(object):
    def __init__(self, process, project_path):
        self.process = process
        self.stdout_thread = threading.Thread(target=self.read_stdout)
        self.stdout_thread.start()
        self.stderr_thread = threading.Thread(target=self.read_stderr)
        self.stderr_thread.start()
        self.project_path = project_path
        self.request_id = 0
        self.handlers = {}  # type: Dict[int, Callable]
        self.capabilities = {}  # type: Dict[str, Any]

    def set_capabilities(self, capabilities):
        self.capabilities = capabilities

    def get_project_path(self):
        return self.project_path

    def has_capability(self, capability):
        return capability in self.capabilities

    def get_capability(self, capability):
        return self.capabilities.get(capability)

    def send_request(self, request: Request, handler: 'Callable'):
        self.request_id += 1
        debug('request {}: {} '.format(self.request_id, request.method))
        if handler is not None:
            self.handlers[self.request_id] = handler
        self.send_payload(request.to_payload(self.request_id))

    def send_notification(self, notification: Notification):
        debug('notify: ' + notification.method)
        self.send_payload(notification.to_payload())

    def kill(self):
        self.process.kill()

    def send_payload(self, payload):
        try:
            message = format_request(payload)
            self.process.stdin.write(bytes(message, 'UTF-8'))
            self.process.stdin.flush()
        except BrokenPipeError as e:
            printf("client unexpectedly died:", e)

    def read_stdout(self):
        """
        Reads JSON responses from process and dispatch them to response_handler
        """
        ContentLengthHeader = b"Content-Length: "

        while self.process.poll() is None:
            try:

                in_headers = True
                content_length = 0
                while in_headers:
                    header = self.process.stdout.readline().strip()
                    if (len(header) == 0):
                        in_headers = False

                    if header.startswith(ContentLengthHeader):
                        content_length = int(header[len(ContentLengthHeader):])

                if (content_length > 0):
                    content = self.process.stdout.read(content_length).decode(
                        "UTF-8")

                    payload = None
                    try:
                        payload = json.loads(content)
                        limit = min(len(content), 200)
                        if payload.get("method") != "window/logMessage":
                            debug("got json: ", content[0:limit])
                    except IOError:
                        printf("Got a non-JSON payload: ", content)
                        continue

                    try:
                        if "error" in payload:
                            error = payload['error']
                            debug("got error: ", error)
                            sublime.status_message(error.get('message'))
                        elif "method" in payload:
                            if "id" in payload:
                                self.request_handler(payload)
                            else:
                                self.notification_handler(payload)
                        elif "id" in payload:
                            self.response_handler(payload)
                        else:
                            debug("Unknown payload type: ", payload)
                    except Exception as err:
                        printf("Error handling server content:", err)

            except IOError:
                printf("LSP stdout process ending due to exception: ",
                       sys.exc_info())
                self.process.terminate()
                self.process = None
                return

        debug("LSP stdout process ended.")

    def read_stderr(self):
        """
        Reads any errors from the LSP process.
        """
        while self.process.poll() is None:
            try:
                content = self.process.stderr.readline()
                if log_stderr and len(content) > 0:
                    printf("(stderr): ", content.strip())
            except IOError:
                printf("LSP stderr process ending due to exception: ",
                       sys.exc_info())
                return

        debug("LSP stderr process ended.")

    def response_handler(self, response):
        try:
            handler_id = int(response.get("id"))  # dotty sends strings back :(
            result = response.get('result', None)
            if (self.handlers[handler_id]):
                self.handlers[handler_id](result)
            else:
                debug("No handler found for id" + response.get("id"))
        except Exception as e:
            debug("error handling response", handler_id)
            raise

    def request_handler(self, request):
        method = request.get("method")
        if method == "workspace/applyEdit":
            apply_workspace_edit(sublime.active_window(),
                                 request.get("params"))
        else:
            debug("Unhandled request", method)

    def notification_handler(self, response):
        method = response.get("method")
        if method == "textDocument/publishDiagnostics":
            Events.publish("document.diagnostics", response.get("params"))
        elif method == "window/showMessage":
            sublime.active_window().message_dialog(
                response.get("params").get("message"))
        elif method == "window/logMessage" and log_server:
            server_log(self.process.args[0],
                       response.get("params").get("message"))
        else:
            debug("Unhandled notification:", method)

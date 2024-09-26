from __future__ import annotations

import io
import weakref
from typing import TYPE_CHECKING, Any, Callable

import requests
import socketio
import torch
from tqdm import tqdm

from ... import CONFIG
from ...logger import logger, remote_logger
from ...tracing import protocols
from .LocalBackend import LocalBackend, LocalMixin

if TYPE_CHECKING:

    from ...schema.Request import RequestModel
    from ...schema.Response import ResponseModel
    from ...tracing.Node import Node


class RemoteMixin(LocalMixin):
    """To be inherited by objects that want to be able to be executed by the RemoteBackend."""

    def remote_backend_get_model_key(self) -> str:
        """Should return the model_key used to specify which model to run on the remote service.

        Returns:
            str: Model key.
        """

        raise NotImplementedError()

    def remote_backend_postprocess_result(self, local_result: Any) -> Any:
        """Should handle postprocessing the result from local_backend_execute.

        For example moving tensors to cpu/detaching/etc.

        Args:
            local_result (Any): Local execution result.

        Returns:
            Any: Post processed local execution result.
        """

        raise NotImplementedError()

    def remote_backend_handle_result_value(self, value: Any) -> None:
        """Should handle postprocessed result from remote_backend_postprocess_result on return from remote service.

        Args:
            value (Any): Result.
        """

        raise NotImplementedError()

    def remote_backend_get_stream_node(self, *args) -> None:
        """Inject streamed data into object."""

        raise NotImplementedError()

    @classmethod
    def remote_stream_format(self, node: Node) -> bytes:

        return node.name, node.graph.id

    def remote_backend_cleanup(self):
        raise NotImplementedError()


class RemoteBackend(LocalBackend):
    """Backend to execute a context object via a remote service.

    Context object must inherit from RemoteMixin and implement its methods.

    Attributes:

        url (str): Remote host url. Defaults to that set in CONFIG.API.HOST.
    """

    def __init__(
        self,
        host: str = None,
        blocking: bool = True,
        job_id: str = None,
        ssl: bool = None,
        api_key: str = "",
    ) -> None:

        self.job_id = job_id or CONFIG.API.JOB_ID
        self.ssl = CONFIG.API.SSL if ssl is None else ssl
        self.api_key = api_key or CONFIG.API.APIKEY
        self.blocking = blocking

        self.host = host or CONFIG.API.HOST
        self.address = f"http{'s' if self.ssl else ''}://{self.host}"
        self.ws_address = f"ws{'s' if CONFIG.API.SSL else ''}://{self.host}"

        self.object: RemoteMixin = None

    def request(self, obj: RemoteMixin):

        model_key = obj.remote_backend_get_model_key()

        from ...schema.Request import RequestModel

        # Create request using pydantic to parse the object itself.
        return RequestModel(object=obj, model_key=model_key)

    def __call__(self, obj: RemoteMixin):

        self.object = weakref.proxy(obj)

        if self.blocking:

            # Do blocking request.
            self.blocking_request(obj)

        else:

            request = None

            if not self.job_id:

                request = self.request(obj)

            self.non_blocking_request(request)

        obj.remote_backend_cleanup()

    def handle_response(self, response: "ResponseModel") -> None:
        """Handles incoming response data.

        Parses it into the `ResponseModel` pydantic object.
        Logs the response object.
        If the job is completed, retrieve and stream the result from the remote endpoint.
        Use torch.load to decode and load the `ResultModel` into memory.
        Use the backend object's .handle_result method to handle the decoded result.

        Args:
            response (Any): Json data to concert to `ResponseModel`

        Raises:
            Exception: If the job's status is `ResponseModel.JobStatus.ERROR`

        Returns:
            ResponseModel: ResponseModel.
        """

        from ...schema.Response import ResponseModel, ResultModel

        # Log response for user
        remote_logger.info(str(response))

        # If the status of the response is completed, update the local nodes that the user specified to save.
        # Then disconnect and continue.
        if response.status == ResponseModel.JobStatus.COMPLETED:
            # Create BytesIO object to store bytes received from server in.
            result_bytes = io.BytesIO()
            result_bytes.seek(0)

            # Get result from result url using job id.
            with requests.get(
                url=f"{self.address}/result/{response.id}",
                stream=True,
            ) as stream:
                # Total size of incoming data.
                total_size = float(stream.headers["Content-length"])

                with tqdm(
                    total=total_size,
                    unit="B",
                    unit_scale=True,
                    desc="Downloading result",
                ) as progress_bar:
                    # chunk_size=None so server determines chunk size.
                    for data in stream.iter_content(chunk_size=None):
                        progress_bar.update(len(data))
                        result_bytes.write(data)

            # Move cursor to beginning of bytes.
            result_bytes.seek(0)

            # Decode bytes with pickle and then into pydantic object.
            result: "ResultModel" = ResultModel(
                **torch.load(
                    result_bytes, map_location="cpu", weights_only=False
                )
            )

            # Close bytes
            result_bytes.close()

            # Handle result
            self.object.remote_backend_handle_result_value(result.value)

        elif response.status == ResponseModel.JobStatus.STREAM:

            *args, value = response.data

            node = self.object.remote_backend_get_stream_node(*args)

            protocols.StreamingProtocol.execute_callback(
                node, value
            )

        # Or if there was some error.
        elif response.status == ResponseModel.JobStatus.ERROR:
            raise Exception(str(response))

        return response

    def submit_request(self, request: "RequestModel") -> "ResponseModel":
        """Sends request to the remote endpoint and handles the response object.

        Raises:
            Exception: If there was a status code other than 200 for the response.

        Returns:
            (ResponseModel): Response.
        """

        from ...schema.Response import ResponseModel

        response = requests.post(
            f"{self.address}/request",
            json=request.model_dump(exclude=["id", "received"]),
            headers={"ndif-api-key": self.api_key},
        )

        if response.status_code == 200:

            response = ResponseModel(**response.json())

            return self.handle_response(response)

        else:

            raise Exception(response.reason)

    def get_response(self) -> "ResponseModel":
        """Retrieves and handles the response object from the remote endpoint.

        Raises:
            Exception: If there was a status code other than 200 for the response.

        Returns:
            (ResponseModel): Response.
        """

        from ...schema.Response import ResponseModel

        response = requests.get(
            f"{self.address}/response/{self.job_id}",
            headers={"ndif-api-key": self.api_key},
        )

        if response.status_code == 200:

            response = ResponseModel(**response.json())

            return self.handle_response(response)

        else:

            raise Exception(response.reason)

    def blocking_request(self, object: RemoteMixin):
        """Send intervention request to the remote service while waiting for updates via websocket.

        Args:
            obj (RemoteMixin): Remote object..
        """

        from ...schema.Response import ResponseModel

        request = self.request(object)

        # Create a socketio connection to the server.
        with socketio.SimpleClient(
            logger=logger, reconnection_attempts=10
        ) as sio:
            # Connect
            sio.connect(
                self.ws_address,
                socketio_path="/ws/socket.io",
                transports=["websocket"],
                wait_timeout=10,
            )

            # Give request session ID so server knows to respond via websockets to us.
            request.session_id = sio.sid

            # Submit request via
            self.submit_request(request)

            # Loop until
            while True:

                response = sio.receive()[1]
                response = ResponseModel.unpickle(response)

                self.handle_response(response)

                if response.status == ResponseModel.JobStatus.COMPLETED:
                    break

    def non_blocking_request(self, request: "RequestModel" = None):
        """Send intervention request to the remote service if request provided. Otherwise get job status.

        Sets CONFIG.API.JOB_ID on initial request as to later get the status of said job.

        When job is completed, clear CONFIG.API.JOB_ID to request a new job.

        Args:
            request (RequestModel): Request if submitting a new request. Defaults to None
        """

        from ...schema.Response import ResponseModel

        if request is not None:

            # Submit request via
            response = self.submit_request(request)

            CONFIG.API.JOB_ID = response.id

            CONFIG.save()

        else:

            try:

                response = self.get_response()

                if response.status == ResponseModel.JobStatus.COMPLETED:

                    CONFIG.API.JOB_ID = None

                    CONFIG.save()

            except Exception as e:

                CONFIG.API.JOB_ID = None

                CONFIG.save()

                raise e

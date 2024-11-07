from __future__ import annotations

import io
import pickle
import weakref
import zlib
import sys
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple

import msgspec
import requests
import socketio
import torch
from tqdm import tqdm

from ... import CONFIG, remote_logger
from ...schema.request import RequestModel
from ...schema.response import ResponseModel
from ...schema.result import RESULT, ResultModel
from ...tracing import protocols
from ...tracing.backends import Backend
from ...tracing.graph import Graph, SubGraph
from .. import protocols as intervention_protocols
from ..contexts.local import LocalContext
from ...util import NNsightError


class RemoteBackend(Backend):
    """Backend to execute a context object via a remote service.

    Context object must inherit from RemoteMixin and implement its methods.

    Attributes:

        url (str): Remote host url. Defaults to that set in CONFIG.API.HOST.
    """

    def __init__(
        self,
        model_key: str,
        host: str = None,
        blocking: bool = True,
        job_id: str = None,
        ssl: bool = None,
        api_key: str = "",
    ) -> None:

        self.model_key = model_key

        self.job_id = job_id or CONFIG.API.JOB_ID
        self.ssl = CONFIG.API.SSL if ssl is None else ssl
        self.zlib = CONFIG.API.ZLIB
        self.format = CONFIG.API.FORMAT
        self.api_key = api_key or CONFIG.API.APIKEY
        self.blocking = blocking

        self.host = host or CONFIG.API.HOST
        self.address = f"http{'s' if self.ssl else ''}://{self.host}"
        self.ws_address = f"ws{'s' if CONFIG.API.SSL else ''}://{self.host}"

    def serialize(self, graph: Graph) -> Tuple[bytes, Dict[str, str]]:

        if self.format == "json":

            data = RequestModel(graph=graph)

            json = data.model_dump(mode="json")

            data = msgspec.json.encode(json)

        elif self.format == "pt":

            data = io.BytesIO()

            torch.save(graph, data)

            data.seek(0)

            data = data.read()

        if self.zlib:

            data = zlib.compress(data)

        headers = {
            "model_key": self.model_key,
            "format": self.format,
            "zlib": str(self.zlib),
        }

        return data, headers

    def __call__(self, graph: Graph):

        if self.blocking:

            # Do blocking request.
            result = self.blocking_request(graph)

        else:

            # Otherwise we are getting the status / result of the existing job.
            result = self.non_blocking_request(graph)
            
        if result is not None:  
            ResultModel.inject(graph, result)

    def handle_response(self, response: ResponseModel, graph:Optional[Graph] = None) -> Optional[RESULT]:
        """Handles incoming response data.

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

        # Log response for user
        response.log(remote_logger)

        # If job is completed:
        if response.status == ResponseModel.JobStatus.COMPLETED:

            # If the response has no result data, it was too big and we need to stream it from the server.
            if response.data is None:

                result = self.get_result(response.id)
            else:

                result = response.data

            return result

        # If were receiving a streamed value:
        elif response.status == ResponseModel.JobStatus.STREAM:

            # Second item is index of LocalContext node.
            # First item is the streamed value from the remote service.

            index, dependencies = response.data
                        
            ResultModel.inject(graph, dependencies)

            node = graph.nodes[index]
            
            node.execute()

        elif response.status == ResponseModel.JobStatus.NNSIGHT_ERROR:
            error_node = graph.nodes[response.data['node_id']]
            print(f"\n{error_node.meta_data['traceback']}")
            sys.tracebacklimit = 0
            raise NNsightError(response.data['message'], error_node.index)
            
            
    def submit_request(self, data: bytes, headers: Dict[str, Any]) -> Optional[ResponseModel]:
        """Sends request to the remote endpoint and handles the response object.

        Raises:
            Exception: If there was a status code other than 200 for the response.

        Returns:
            (ResponseModel): Response.
        """

        from ...schema.response import ResponseModel

        headers["Content-Type"] = "application/octet-stream"

        response = requests.post(
            f"{self.address}/request",
            data=data,
            headers=headers,
        )

        if response.status_code == 200:

            response = ResponseModel(**response.json())

            self.handle_response(response)
            
            return response

        else:
            msg = response.json()["detail"]
            raise ConnectionError(msg)

    def get_response(self) -> Optional[RESULT]:
        """Retrieves and handles the response object from the remote endpoint.

        Raises:
            Exception: If there was a status code other than 200 for the response.

        Returns:
            (ResponseModel): Response.
        """

        from ...schema.response import ResponseModel

        response = requests.get(
            f"{self.address}/response/{self.job_id}",
            headers={"ndif-api-key": self.api_key},
        )

        if response.status_code == 200:

            response = ResponseModel(**response.json())

            return self.handle_response(response)

        else:

            raise Exception(response.reason)

    def get_result(self, id: str) -> RESULT:

        result_bytes = io.BytesIO()
        result_bytes.seek(0)

        # Get result from result url using job id.
        with requests.get(
            url=f"{self.address}/result/{id}",
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
        result = torch.load(result_bytes, map_location="cpu", weights_only=False)

        result = ResultModel(**result).result

        # Close bytes
        result_bytes.close()

        return result

    def blocking_request(self, graph: Graph) -> Optional[RESULT]:
        """Send intervention request to the remote service while waiting for updates via websocket.

        Args:
            request (RequestModel):Request.
        """

        # We need to do some processing / optimizations on both the graph were sending remotely
        # and our local intervention graph. In order handle the more complex Protocols for streaming.

        # Create a socketio connection to the server.
        with socketio.SimpleClient(reconnection_attempts=10) as sio:
            # Connect
            sio.connect(
                self.ws_address,
                socketio_path="/ws/socket.io",
                transports=["websocket"],
                wait_timeout=10,
            )
            
            remote_graph = preprocess(graph)

            data, headers = self.serialize(remote_graph)
            
            headers['session_id'] = sio.sid
            
            # Submit request via
            self.submit_request(data, headers)

            # We need to tell the StreamingUploadProtocol how to use our websocket connection
            # so it can upload values during execution to our job.
            # protocols.StreamingUploadProtocol.set(
            #     lambda *args: self.stream_send(
            #         *args, job_id=response.id, sio=sio
            #     )
            # )

            try:
                # Loop until
                while True:

                    # Get pickled bytes value from the websocket.
                    response = sio.receive()[1]
                    # Convert to pydantic object.
                    response = ResponseModel.unpickle(response)
                    # Handle the response.
                    result = self.handle_response(response, graph=graph)
                    # Break when completed.
                    if result is not None:
                        return result

            except Exception as e:

                raise e

            finally:
                pass

                # Clear StreamingUploadProtocol state
                # protocols.StreamingUploadProtocol.set(None)

    def stream_send(self, value: Any, job_id: str, sio: socketio.SimpleClient):
        """Upload some value to the remote service for some job id.

        Args:
            value (Any): Value to upload
            job_id (str): Job id.
            sio (socketio.SimpleClient): Connected websocket client.
        """

        from ...schema.request import StreamValueModel

        request = StreamValueModel(value=value)

        sio.emit("stream_upload", data=(request.model_dump_json(), job_id))

    def non_blocking_request(self, graph: Graph):
        """Send intervention request to the remote service if request provided. Otherwise get job status.

        Sets CONFIG.API.JOB_ID on initial request as to later get the status of said job.

        When job is completed, clear CONFIG.API.JOB_ID to request a new job.

        Args:
            request (RequestModel): Request if submitting a new request. Defaults to None
        """

        from ...schema.response import ResponseModel

        if self.job_id is None:
            
            data, headers = self.serialize(graph)

            # Submit request via
            response = self.submit_request(data, headers)

            CONFIG.API.JOB_ID = response.id

            CONFIG.save()

        else:

            try:

                result = self.get_response()

                if result is not None:

                    CONFIG.API.JOB_ID = None

                    CONFIG.save()
                    
                    return result

            except Exception as e:

                CONFIG.API.JOB_ID = None

                CONFIG.save()

                raise e

def preprocess(graph: Graph):
        
    new_graph = graph.copy()
    
    for node in new_graph.nodes:
        
        if node.target is LocalContext:
            
            LocalContext.prune(node)
     
    return new_graph
            
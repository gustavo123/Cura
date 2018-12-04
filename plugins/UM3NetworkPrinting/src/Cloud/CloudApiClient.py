# Copyright (c) 2018 Ultimaker B.V.
# Cura is released under the terms of the LGPLv3 or higher.
import json
from json import JSONDecodeError
from typing import Callable, List, Type, TypeVar, Union, Optional, Tuple, Dict

from PyQt5.QtNetwork import QNetworkRequest, QNetworkReply

from UM.Logger import Logger
from cura.API import Account
from cura.NetworkClient import NetworkClient
from plugins.UM3NetworkPrinting.src.Models import BaseModel
from plugins.UM3NetworkPrinting.src.Cloud.Models import (
    CloudCluster, CloudErrorObject, CloudClusterStatus, CloudJobUploadRequest,
    CloudJobResponse,
    CloudPrintResponse
)


## The cloud API client is responsible for handling the requests and responses from the cloud.
#  Each method should only handle models instead of exposing any HTTP details.
class CloudApiClient(NetworkClient):

    # The cloud URL to use for this remote cluster.
    # TODO: Make sure that this URL goes to the live api before release
    ROOT_PATH = "https://api-staging.ultimaker.com"
    CLUSTER_API_ROOT = "{}/connect/v1/".format(ROOT_PATH)
    CURA_API_ROOT = "{}/cura/v1/".format(ROOT_PATH)

    ## Initializes a new cloud API client.
    #  \param account: The user's account object
    #  \param on_error: The callback to be called whenever we receive errors from the server.
    def __init__(self, account: Account, on_error: Callable[[List[CloudErrorObject]], None]):
        super().__init__()
        self._account = account
        self._on_error = on_error

    ## Retrieves all the clusters for the user that is currently logged in.
    #  \param on_finished: The function to be called after the result is parsed.
    def getClusters(self, on_finished: Callable[[List[CloudCluster]], any]) -> None:
        url = "/clusters"
        self.get(url, on_finished=self._createCallback(on_finished, CloudCluster))

    ## Retrieves the status of the given cluster.
    #  \param cluster_id: The ID of the cluster.
    #  \param on_finished: The function to be called after the result is parsed.
    def getClusterStatus(self, cluster_id: str, on_finished: Callable[[CloudClusterStatus], any]) -> None:
        url = "{}/cluster/{}/status".format(self.CLUSTER_API_ROOT, cluster_id)
        self.get(url, on_finished=self._createCallback(on_finished, CloudClusterStatus))

    ## Requests the cloud to register the upload of a print job mesh.
    #  \param request: The request object.
    #  \param on_finished: The function to be called after the result is parsed.
    def requestUpload(self, request: CloudJobUploadRequest, on_finished: Callable[[CloudJobResponse], any]) -> None:
        url = "{}/jobs/upload".format(self.CURA_API_ROOT)
        body = json.dumps({"data": request.__dict__})
        self.put(url, body, on_finished=self._createCallback(on_finished, CloudJobResponse))

    ## Requests the cloud to register the upload of a print job mesh.
    #  \param upload_response: The object received after requesting an upload with `self.requestUpload`.
    #  \param on_finished: The function to be called after the result is parsed. It receives the print job ID.
    def uploadMesh(self, upload_response: CloudJobResponse, mesh: bytes, on_finished: Callable[[str], any],
                   on_progress: Callable[[int], any]):
        
        def progressCallback(bytes_sent: int, bytes_total: int) -> None:
            if bytes_total:
                on_progress(int((bytes_sent / bytes_total) * 100))

        def finishedCallback(reply: QNetworkReply):
            status_code, response = self._parseReply(reply)
            if status_code < 300:
                on_finished(upload_response.job_id)
            else:
                self._uploadMeshError(status_code, response)

        # TODO: Multipart upload
        self.put(upload_response.upload_url, data = mesh, content_type = upload_response.content_type,
                 on_finished = finishedCallback, on_progress = progressCallback)

    # Requests a cluster to print the given print job.
    #  \param cluster_id: The ID of the cluster.
    #  \param job_id: The ID of the print job.
    #  \param on_finished: The function to be called after the result is parsed.
    def requestPrint(self, cluster_id: str, job_id: str, on_finished: Callable[[], any]) -> None:
        url = "{}/cluster/{}/print/{}".format(self.CLUSTER_API_ROOT, cluster_id, job_id)
        self.post(url, data = "", on_finished=self._createCallback(on_finished, CloudPrintResponse))

    ##  We override _createEmptyRequest in order to add the user credentials.
    #   \param url: The URL to request
    #   \param content_type: The type of the body contents.
    def _createEmptyRequest(self, path: str, content_type: Optional[str] = "application/json") -> QNetworkRequest:
        request = super()._createEmptyRequest(path, content_type)
        if self._account.isLoggedIn:
            request.setRawHeader(b"Authorization", "Bearer {}".format(self._account.accessToken).encode())
        return request

    ## Parses the given JSON network reply into a status code and a dictionary, handling unexpected errors as well.
    #  \param reply: The reply from the server.
    #  \return A tuple with a status code and a dictionary.
    @staticmethod
    def _parseReply(reply: QNetworkReply) -> Tuple[int, Dict[str, any]]:
        status_code = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        try:
            response = bytes(reply.readAll()).decode()
            Logger.log("i", "Received an HTTP %s from %s with %s", status_code, reply.url, response)
            return status_code, json.loads(response)
        except (UnicodeDecodeError, JSONDecodeError, ValueError) as err:
            error = {"code": type(err).__name__, "title": str(err), "http_code": str(status_code)}
            Logger.logException("e", "Could not parse the stardust response: %s", error)
            return status_code, {"errors": [error]}

    ## Calls the error handler that is responsible for handling errors uploading meshes.
    #  \param http_status - The status of the HTTP request.
    #  \param response - The response received from the upload endpoint. This is not formatted according to the standard
    #       JSON-api response.
    def _uploadMeshError(self, http_status: int, response: Dict[str, any]) -> None:
        error = CloudErrorObject(
            code = "uploadError",
            http_status = str(http_status),
            title = "Could not upload the mesh",
            meta = response
        )
        self._on_error([error])

    ## The generic type variable used to document the methods below.
    Model = TypeVar("Model", bound=BaseModel)

    ## Parses the given models and calls the correct callback depending on the result.
    #  \param response: The response from the server, after being converted to a dict.
    #  \param on_finished: The callback in case the response is successful.
    #  \param model: The type of the model to convert the response to. It may either be a single record or a list.
    def _parseModels(self, response: Dict[str, any],
                     on_finished: Callable[[Union[Model, List[Model]]], any],
                     model: Type[Model]) -> None:
        if "data" in response:
            data = response["data"]
            result = [model(**c) for c in data] if isinstance(data, list) else model(**data)
            on_finished(result)
        elif "error" in response:
            self._on_error([CloudErrorObject(**error) for error in response["errors"]])
        else:
            Logger.log("e", "Cannot find data or errors in the cloud response: %s", response)

    ## Creates a callback function that includes the parsing of the response into the correct model.
    #  \param on_finished: The callback in case the response is successful.
    #  \param model: The type of the model to convert the response to. It may either be a single record or a list.
    #  \return: A function that can be passed to the
    def _createCallback(self,
                        on_finished: Callable[[Union[Model, List[Model]]], any],
                        model: Type[Model],
                        ) -> Callable[[QNetworkReply], None]:
        def parse(reply: QNetworkReply) -> None:
            status_code, response = self._parseReply(reply)
            return self._parseModels(response, on_finished, model)
        return parse

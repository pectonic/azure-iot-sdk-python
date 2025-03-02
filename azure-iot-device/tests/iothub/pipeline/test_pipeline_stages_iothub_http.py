# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------
import logging
import pytest
import json
import sys
import urllib
from azure.iot.device.common.pipeline import pipeline_ops_http
from azure.iot.device.iothub.pipeline import (
    pipeline_ops_iothub_http,
    pipeline_stages_iothub_http,
    config,
)
from azure.iot.device.exceptions import ServiceError
from tests.common.pipeline.helpers import StageRunOpTestBase
from tests.common.pipeline import pipeline_stage_test
from azure.iot.device import constant as pkg_constant
from azure.iot.device import user_agent

logging.basicConfig(level=logging.DEBUG)
pytestmark = pytest.mark.usefixtures("fake_pipeline_thread")
this_module = sys.modules[__name__]

###################
# COMMON FIXTURES #
###################


@pytest.fixture(params=[True, False], ids=["With error", "No error"])
def op_error(request, arbitrary_exception):
    if request.param:
        return arbitrary_exception
    else:
        return None


@pytest.fixture
def mock_http_path_iothub(mocker):
    mock = mocker.patch(
        "azure.iot.device.iothub.pipeline.pipeline_stages_iothub_http.http_path_iothub"
    )
    return mock


##################################
# IOT HUB HTTP TRANSLATION STAGE #
##################################


class IoTHubHTTPTranslationStageTestConfig(object):
    @pytest.fixture
    def cls_type(self):
        return pipeline_stages_iothub_http.IoTHubHTTPTranslationStage

    @pytest.fixture
    def init_kwargs(self):
        return {}

    @pytest.fixture
    def pipeline_config(self, mocker):
        # auth type shouldn't matter for this stage, so just give it a fake sastoken for now.
        # Manually override to make this for modules
        cfg = config.IoTHubPipelineConfig(
            hostname="http://my.hostname", device_id="my_device", sastoken=mocker.MagicMock()
        )
        return cfg

    @pytest.fixture
    def stage(self, mocker, cls_type, init_kwargs, nucleus, pipeline_config):
        stage = cls_type(**init_kwargs)
        stage.nucleus = nucleus
        stage.nucleus.pipeline_configuration = pipeline_config
        stage.send_op_down = mocker.MagicMock()
        stage.send_event_up = mocker.MagicMock()
        mocker.spy(stage, "report_background_exception")
        return stage


pipeline_stage_test.add_base_pipeline_stage_tests(
    test_module=this_module,
    stage_class_under_test=pipeline_stages_iothub_http.IoTHubHTTPTranslationStage,
    stage_test_config_class=IoTHubHTTPTranslationStageTestConfig,
)


@pytest.mark.describe(
    "IoTHubHTTPTranslationStage - .run_op() -- Called with MethodInvokeOperation op"
)
class TestIoTHubHTTPTranslationStageRunOpCalledWithMethodInvokeOperation(
    IoTHubHTTPTranslationStageTestConfig, StageRunOpTestBase
):
    @pytest.fixture
    def pipeline_config(self, mocker):
        # Because Method related functionality is limited to Module, configure the stage for a module
        # auth type shouldn't matter for this stage, so just give it a fake sastoken for now.
        cfg = config.IoTHubPipelineConfig(
            hostname="http://my.hostname",
            gateway_hostname="http://my.gateway.hostname",
            device_id="my_device",
            module_id="my_module",
            sastoken=mocker.MagicMock(),
        )
        return cfg

    @pytest.fixture(params=["Targeting Device Method", "Targeting Module Method"])
    def op(self, mocker, request):
        method_params = {"arg1": "val", "arg2": 2, "arg3": True}
        if request.param == "Targeting Device Method":
            return pipeline_ops_iothub_http.MethodInvokeOperation(
                target_device_id="fake_target_device_id",
                target_module_id=None,
                method_params=method_params,
                callback=mocker.MagicMock(),
            )
        else:
            return pipeline_ops_iothub_http.MethodInvokeOperation(
                target_device_id="fake_target_device_id",
                target_module_id="fake_target_module_id",
                method_params=method_params,
                callback=mocker.MagicMock(),
            )

    @pytest.mark.it("Sends a new HTTPRequestAndResponseOperation op down the pipeline")
    def test_sends_op_down(self, mocker, stage, op):
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

    @pytest.mark.it(
        "Configures the HTTPRequestAndResponseOperation with request details for sending a Method Invoke request"
    )
    def test_sends_get_storage_request(self, mocker, stage, op, mock_http_path_iothub):
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

        # Validate request
        assert mock_http_path_iothub.get_method_invoke_path.call_count == 1
        assert mock_http_path_iothub.get_method_invoke_path.call_args == mocker.call(
            op.target_device_id, op.target_module_id
        )
        expected_path = mock_http_path_iothub.get_method_invoke_path.return_value

        assert new_op.method == "POST"
        assert new_op.path == expected_path
        assert new_op.query_params == "api-version={}".format(pkg_constant.IOTHUB_API_VERSION)

    @pytest.mark.it(
        "Configures the HTTPRequestAndResponseOperation with the headers for a Method Invoke request"
    )
    @pytest.mark.parametrize(
        "custom_user_agent",
        [
            pytest.param("", id="No custom user agent"),
            pytest.param("MyCustomUserAgent", id="With custom user agent"),
            pytest.param(
                "My/Custom?User+Agent", id="With custom user agent containing reserved characters"
            ),
            pytest.param(12345, id="Non-string custom user agent"),
        ],
    )
    def test_new_op_headers(self, mocker, stage, op, custom_user_agent, pipeline_config):
        stage.nucleus.pipeline_configuration.product_info = custom_user_agent
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

        # Validate headers
        expected_user_agent = urllib.parse.quote_plus(
            user_agent.get_iothub_user_agent() + str(custom_user_agent)
        )
        expected_edge_string = "{}/{}".format(pipeline_config.device_id, pipeline_config.module_id)

        assert new_op.headers["Host"] == pipeline_config.gateway_hostname
        assert new_op.headers["Content-Type"] == "application/json"
        assert new_op.headers["Content-Length"] == str(len(new_op.body))
        assert new_op.headers["x-ms-edge-moduleId"] == expected_edge_string
        assert new_op.headers["User-Agent"] == expected_user_agent

    @pytest.mark.it(
        "Configures the HTTPRequestAndResponseOperation with a body for a Method Invoke request"
    )
    def test_new_op_body(self, mocker, stage, op):
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

        # Validate body
        assert new_op.body == json.dumps(op.method_params)

    @pytest.mark.it(
        "Completes the original MethodInvokeOperation op (no error) if the new HTTPRequestAndResponseOperation op is completed later on (no error) with a status code indicating success"
    )
    def test_new_op_completes_with_good_code(self, mocker, stage, op):
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

        # Neither op is completed
        assert not op.completed
        assert op.error is None
        assert not new_op.completed
        assert new_op.error is None

        # Complete new op
        new_op.response_body = b'{"some_response_key": "some_response_value"}'
        new_op.status_code = 200
        new_op.complete()

        # Both ops are now completed successfully
        assert new_op.completed
        assert new_op.error is None
        assert op.completed
        assert op.error is None

    @pytest.mark.it(
        "Deserializes the completed HTTPRequestAndResponseOperation op's 'response_body' (the received storage info) and set it on the MethodInvokeOperation op as the 'method_response', if the HTTPRequestAndResponseOperation is completed later (no error) with a status code indicating success"
    )
    @pytest.mark.parametrize(
        "response_body, expected_method_response",
        [
            pytest.param(
                b'{"key": "val"}', {"key": "val"}, id="Response Body: dict value as bytestring"
            ),
            pytest.param(
                b'{"key": "val", "key2": {"key3": "val2"}}',
                {"key": "val", "key2": {"key3": "val2"}},
                id="Response Body: dict value as bytestring",
            ),
        ],
    )
    def test_deserializes_response(
        self, mocker, stage, op, response_body, expected_method_response
    ):
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

        # Original op has no 'method_response'
        assert op.method_response is None

        # Complete new op
        new_op.response_body = response_body
        new_op.status_code = 200
        new_op.complete()

        # Method Response is set
        assert op.method_response == expected_method_response

    @pytest.mark.it(
        "Completes the original MethodInvokeOperation op with a ServiceError if the new HTTPRequestAndResponseOperation is completed later on (no error) with a status code indicating non-success"
    )
    @pytest.mark.parametrize(
        "status_code",
        [
            pytest.param(300, id="Status Code: 300"),
            pytest.param(400, id="Status Code: 400"),
            pytest.param(500, id="Status Code: 500"),
        ],
    )
    def test_new_op_completes_with_bad_code(self, mocker, stage, op, status_code):
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

        # Neither op is completed
        assert not op.completed
        assert op.error is None
        assert not new_op.completed
        assert new_op.error is None

        # Complete new op successfully (but with a bad status code)
        new_op.status_code = status_code
        new_op.complete()

        # The original op is now completed with a ServiceError
        assert new_op.completed
        assert new_op.error is None
        assert op.completed
        assert isinstance(op.error, ServiceError)

    @pytest.mark.it(
        "Completes the original MethodInvokeOperation op with the error from the new HTTPRequestAndResponseOperation, if the HTTPRequestAndResponseOperation is completed later on with error"
    )
    def test_new_op_completes_with_error(self, mocker, stage, op, arbitrary_exception):
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

        # Neither op is completed
        assert not op.completed
        assert op.error is None
        assert not new_op.completed
        assert new_op.error is None

        # Complete new op with error
        new_op.complete(error=arbitrary_exception)

        # The original op is now completed with a ServiceError
        assert new_op.completed
        assert new_op.error is arbitrary_exception
        assert op.completed
        assert op.error is arbitrary_exception


@pytest.mark.describe(
    "IoTHubHTTPTranslationStage - .run_op() -- Called with GetStorageInfoOperation op"
)
class TestIoTHubHTTPTranslationStageRunOpCalledWithGetStorageInfoOperation(
    IoTHubHTTPTranslationStageTestConfig, StageRunOpTestBase
):
    @pytest.fixture
    def pipeline_config(self, mocker):
        # Because Storage/Blob related functionality is limited to Device, configure pipeline for a device
        # auth type shouldn't matter for this stage, so just give it a fake sastoken for now.
        cfg = config.IoTHubPipelineConfig(
            hostname="http://my.hostname", device_id="my_device", sastoken=mocker.MagicMock()
        )
        return cfg

    @pytest.fixture
    def op(self, mocker):
        return pipeline_ops_iothub_http.GetStorageInfoOperation(
            blob_name="fake_blob_name", callback=mocker.MagicMock()
        )

    @pytest.mark.it("Sends a new HTTPRequestAndResponseOperation op down the pipeline")
    def test_sends_op_down(self, mocker, stage, op):
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

    @pytest.mark.it(
        "Configures the HTTPRequestAndResponseOperation with request details for sending a Get Storage Info request"
    )
    def test_sends_get_storage_request(
        self, mocker, stage, op, mock_http_path_iothub, pipeline_config
    ):
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

        # Validate request
        assert mock_http_path_iothub.get_storage_info_for_blob_path.call_count == 1
        assert mock_http_path_iothub.get_storage_info_for_blob_path.call_args == mocker.call(
            pipeline_config.device_id
        )
        expected_path = mock_http_path_iothub.get_storage_info_for_blob_path.return_value

        assert new_op.method == "POST"
        assert new_op.path == expected_path
        assert new_op.query_params == "api-version={}".format(pkg_constant.IOTHUB_API_VERSION)

    @pytest.mark.it(
        "Configures the HTTPRequestAndResponseOperation with the headers for a Get Storage Info request"
    )
    @pytest.mark.parametrize(
        "custom_user_agent",
        [
            pytest.param("", id="No custom user agent"),
            pytest.param("MyCustomUserAgent", id="With custom user agent"),
            pytest.param(
                "My/Custom?User+Agent", id="With custom user agent containing reserved characters"
            ),
            pytest.param(12345, id="Non-string custom user agent"),
        ],
    )
    def test_new_op_headers(self, mocker, stage, op, custom_user_agent, pipeline_config):
        stage.nucleus.pipeline_configuration.product_info = custom_user_agent
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

        # Validate headers
        expected_user_agent = urllib.parse.quote_plus(
            user_agent.get_iothub_user_agent() + str(custom_user_agent)
        )

        assert new_op.headers["Host"] == pipeline_config.hostname
        assert new_op.headers["Accept"] == "application/json"
        assert new_op.headers["Content-Type"] == "application/json"
        assert new_op.headers["Content-Length"] == str(len(new_op.body))
        assert new_op.headers["User-Agent"] == expected_user_agent

    @pytest.mark.it(
        "Configures the HTTPRequestAndResponseOperation with a body for a Get Storage Info request"
    )
    def test_new_op_body(self, mocker, stage, op):
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

        # Validate body
        assert new_op.body == '{{"blobName": "{}"}}'.format(op.blob_name)

    @pytest.mark.it(
        "Completes the original GetStorageInfoOperation op (no error) if the new HTTPRequestAndResponseOperation is completed later on (no error) with a status code indicating success"
    )
    def test_new_op_completes_with_good_code(self, mocker, stage, op):
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

        # Neither op is completed
        assert not op.completed
        assert op.error is None
        assert not new_op.completed
        assert new_op.error is None

        # Complete new op
        new_op.response_body = b'{"json": "response"}'
        new_op.status_code = 200
        new_op.complete()

        # Both ops are now completed successfully
        assert new_op.completed
        assert new_op.error is None
        assert op.completed
        assert op.error is None

    @pytest.mark.it(
        "Deserializes the completed HTTPRequestAndResponseOperation op's 'response_body' (the received storage info) and set it on the GetStorageInfoOperation as the 'storage_info', if the HTTPRequestAndResponseOperation is completed later (no error) with a status code indicating success"
    )
    def test_deserializes_response(self, mocker, stage, op):
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

        # Original op has no 'storage_info'
        assert op.storage_info is None

        # Complete new op
        new_op.response_body = b'{\
            "hostName": "fake_hostname",\
            "containerName": "fake_container_name",\
            "blobName": "fake_blob_name",\
            "sasToken": "fake_sas_token",\
            "correlationId": "fake_correlation_id"\
        }'
        new_op.status_code = 200
        new_op.complete()

        # Storage Info is set
        assert op.storage_info == {
            "hostName": "fake_hostname",
            "containerName": "fake_container_name",
            "blobName": "fake_blob_name",
            "sasToken": "fake_sas_token",
            "correlationId": "fake_correlation_id",
        }

    @pytest.mark.it(
        "Completes the original GetStorageInfoOperation op with a ServiceError if the new HTTPRequestAndResponseOperation is completed later on (no error) with a status code indicating non-success"
    )
    @pytest.mark.parametrize(
        "status_code",
        [
            pytest.param(300, id="Status Code: 300"),
            pytest.param(400, id="Status Code: 400"),
            pytest.param(500, id="Status Code: 500"),
        ],
    )
    def test_new_op_completes_with_bad_code(self, mocker, stage, op, status_code):
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

        # Neither op is completed
        assert not op.completed
        assert op.error is None
        assert not new_op.completed
        assert new_op.error is None

        # Complete new op successfully (but with a bad status code)
        new_op.status_code = status_code
        new_op.complete()

        # The original op is now completed with a ServiceError
        assert new_op.completed
        assert new_op.error is None
        assert op.completed
        assert isinstance(op.error, ServiceError)

    @pytest.mark.it(
        "Completes the original GetStorageInfoOperation op with the error from the new HTTPRequestAndResponseOperation, if the HTTPRequestAndResponseOperation is completed later on with error"
    )
    def test_new_op_completes_with_error(self, mocker, stage, op, arbitrary_exception):
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

        # Neither op is completed
        assert not op.completed
        assert op.error is None
        assert not new_op.completed
        assert new_op.error is None

        # Complete new op with error
        new_op.complete(error=arbitrary_exception)

        # The original op is now completed with a ServiceError
        assert new_op.completed
        assert new_op.error is arbitrary_exception
        assert op.completed
        assert op.error is arbitrary_exception


@pytest.mark.describe(
    "IoTHubHTTPTranslationStage - .run_op() -- Called with NotifyBlobUploadStatusOperation op"
)
class TestIoTHubHTTPTranslationStageRunOpCalledWithNotifyBlobUploadStatusOperation(
    IoTHubHTTPTranslationStageTestConfig, StageRunOpTestBase
):
    @pytest.fixture
    def pipeline_config(self, mocker):
        # Because Storage/Blob related functionality is limited to Device, configure pipeline for a device
        # auth type shouldn't matter for this stage, so just give it a fake sastoken for now.
        cfg = config.IoTHubPipelineConfig(
            hostname="http://my.hostname", device_id="my_device", sastoken=mocker.MagicMock()
        )
        return cfg

    @pytest.fixture
    def op(self, mocker):
        return pipeline_ops_iothub_http.NotifyBlobUploadStatusOperation(
            correlation_id="fake_correlation_id",
            is_success=True,
            status_code=203,
            status_description="fake_description",
            callback=mocker.MagicMock(),
        )

    @pytest.mark.it("Sends a new HTTPRequestAndResponseOperation op down the pipeline")
    def test_sends_op_down(self, mocker, stage, op):
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

    @pytest.mark.it(
        "Configures the HTTPRequestAndResponseOperation with request details for sending a Notify Blob Upload Status request"
    )
    def test_sends_get_storage_request(
        self, mocker, stage, op, mock_http_path_iothub, pipeline_config
    ):
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

        # Validate request
        assert mock_http_path_iothub.get_notify_blob_upload_status_path.call_count == 1
        assert mock_http_path_iothub.get_notify_blob_upload_status_path.call_args == mocker.call(
            pipeline_config.device_id
        )
        expected_path = mock_http_path_iothub.get_notify_blob_upload_status_path.return_value

        assert new_op.method == "POST"
        assert new_op.path == expected_path
        assert new_op.query_params == "api-version={}".format(pkg_constant.IOTHUB_API_VERSION)

    @pytest.mark.it(
        "Configures the HTTPRequestAndResponseOperation with the headers for a Notify Blob Upload Status request"
    )
    @pytest.mark.parametrize(
        "custom_user_agent",
        [
            pytest.param("", id="No custom user agent"),
            pytest.param("MyCustomUserAgent", id="With custom user agent"),
            pytest.param(
                "My/Custom?User+Agent", id="With custom user agent containing reserved characters"
            ),
            pytest.param(12345, id="Non-string custom user agent"),
        ],
    )
    def test_new_op_headers(self, mocker, stage, op, custom_user_agent, pipeline_config):
        stage.nucleus.pipeline_configuration.product_info = custom_user_agent
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

        # Validate headers
        expected_user_agent = urllib.parse.quote_plus(
            user_agent.get_iothub_user_agent() + str(custom_user_agent)
        )

        assert new_op.headers["Host"] == pipeline_config.hostname
        assert new_op.headers["Content-Type"] == "application/json; charset=utf-8"
        assert new_op.headers["Content-Length"] == str(len(new_op.body))
        assert new_op.headers["User-Agent"] == expected_user_agent

    @pytest.mark.it(
        "Configures the HTTPRequestAndResponseOperation with a body for a Notify Blob Upload Status request"
    )
    def test_new_op_body(self, mocker, stage, op):
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

        # Validate body
        header_dict = {
            "correlationId": op.correlation_id,
            "isSuccess": op.is_success,
            "statusCode": op.request_status_code,
            "statusDescription": op.status_description,
        }
        assert new_op.body == json.dumps(header_dict)

    @pytest.mark.it(
        "Completes the original NotifyBlobUploadStatusOperation op (no error) if the new HTTPRequestAndResponseOperation is completed later on (no error) with a status code indicating success"
    )
    def test_new_op_completes_with_good_code(self, mocker, stage, op):
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

        # Neither op is completed
        assert not op.completed
        assert op.error is None
        assert not new_op.completed
        assert new_op.error is None

        # Complete new op
        new_op.status_code = 200
        new_op.complete()

        # Both ops are now completed successfully
        assert new_op.completed
        assert new_op.error is None
        assert op.completed
        assert op.error is None

    @pytest.mark.it(
        "Completes the original NotifyBlobUploadStatusOperation op with a ServiceError if the new HTTPRequestAndResponseOperation is completed later on (no error) with a status code indicating non-success"
    )
    @pytest.mark.parametrize(
        "status_code",
        [
            pytest.param(300, id="Status Code: 300"),
            pytest.param(400, id="Status Code: 400"),
            pytest.param(500, id="Status Code: 500"),
        ],
    )
    def test_new_op_completes_with_bad_code(self, mocker, stage, op, status_code):
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

        # Neither op is completed
        assert not op.completed
        assert op.error is None
        assert not new_op.completed
        assert new_op.error is None

        # Complete new op successfully (but with a bad status code)
        new_op.status_code = status_code
        new_op.complete()

        # The original op is now completed with a ServiceError
        assert new_op.completed
        assert new_op.error is None
        assert op.completed
        assert isinstance(op.error, ServiceError)

    @pytest.mark.it(
        "Completes the original NotifyBlobUploadStatusOperation op with the error from the new HTTPRequestAndResponseOperation, if the HTTPRequestAndResponseOperation is completed later on with error"
    )
    def test_new_op_completes_with_error(self, mocker, stage, op, arbitrary_exception):
        stage.run_op(op)

        # Op was sent down
        assert stage.send_op_down.call_count == 1
        new_op = stage.send_op_down.call_args[0][0]
        assert isinstance(new_op, pipeline_ops_http.HTTPRequestAndResponseOperation)

        # Neither op is completed
        assert not op.completed
        assert op.error is None
        assert not new_op.completed
        assert new_op.error is None

        # Complete new op with error
        new_op.complete(error=arbitrary_exception)

        # The original op is now completed with a ServiceError
        assert new_op.completed
        assert new_op.error is arbitrary_exception
        assert op.completed
        assert op.error is arbitrary_exception

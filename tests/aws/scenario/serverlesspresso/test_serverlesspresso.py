"""
This scenario setup is based on the official AWS serverlesspresso sample available at https://workshop.serverlesscoffee.com/

Source: https://github.com/aws-samples/serverless-coffee-workshop

It's originally written via SAM but has been adapted here into a Python-based CDK application.

"""

import base64
import json
import os
import time

import aws_cdk as cdk
import pytest

from localstack.testing.aws.util import is_aws_cloud
from localstack.testing.pytest import markers
from localstack.testing.scenario.provisioning import InfraProvisioner, cleanup_s3_bucket
from localstack.utils.strings import short_uid, to_str
from localstack.utils.sync import retry
from tests.aws.scenario.serverlesspresso.constructs.serverlesspresso_core_stack import (
    SERVERLESSPRESSO_SOURCE,
    ServerlesspressoCoreStack,
)
from tests.aws.services.stepfunctions.utils import await_execution_terminated

STACK_NAME = "ServerlesspressoStack"


@pytest.mark.skipif(condition=not is_aws_cloud(), reason="not working in too many places")
class TestServerlesspressoScenario:
    """

    Components:
    The Counting microservice - Uses an Amazon DynamoDB table for incrementing order numbers sequentially.
    The OrderManager microservice - Provides an API to send/update/cancel a coffee order. Consists of a DynamoDB table containing the state of each customer order.
    The Config microservice - Uses a DynamoDB table containing information about menu items and shop status, along with an Amazon API Gateway resource to provide authenticated access.
    The Publisher microservice - Routes events to different IoT core topics. IoT Core publishes event messages to front-end applications.
    The QR Validator microservice - Provides QR codes to front end display application, Codes are sorted in a DynamoDB table and used to validate each order.

    """

    @pytest.fixture(scope="class", autouse=True)
    def infrastructure(self, aws_client):
        # ================================================================================================
        # upload API files
        # ================================================================================================

        bucket_name = f"serverlesspresso-localstack-testing123"
        aws_client.s3.create_bucket(Bucket=bucket_name)
        aws_client.s3.upload_file(
            Filename=os.path.join(
                os.path.dirname(__file__), "artifacts/api/order_manager_api.yaml"
            ),
            Bucket=bucket_name,
            Key="api/order_manager_api.yaml",
        )
        aws_client.s3.upload_file(
            Filename=os.path.join(
                os.path.dirname(__file__), "artifacts/api/config_service_api.yaml"
            ),
            Bucket=bucket_name,
            Key="api/config_service_api.yaml",
        )

        # ================================================================================================
        # CDK App/stack
        # ================================================================================================

        app = cdk.App(auto_synth=False)
        stack = ServerlesspressoCoreStack(app, STACK_NAME, bucket_name=bucket_name)

        # ================================================================================================
        # Provisioner setup
        # ================================================================================================

        provisioner = InfraProvisioner(aws_client)
        provisioner.add_cdk_stack(stack)
        provisioner.add_custom_teardown(lambda: cleanup_s3_bucket(aws_client.s3, bucket_name))
        provisioner.add_custom_teardown(lambda: aws_client.s3.delete_bucket(Bucket=bucket_name))
        with provisioner.provisioner(skip_teardown=True) as prov:
            yield prov

    def _change_store_state(self, aws_client, infrastructure, new_state: bool):
        outputs = infrastructure.get_stack_outputs(stack_name=STACK_NAME)
        config_table_name = outputs["ConfigTableName"]
        aws_client.dynamodb.update_item(
            TableName=config_table_name,
            Key={"PK": {"S": "config"}},
            UpdateExpression="set storeOpen = :val1",
            ExpressionAttributeValues={":val1": {"BOOL": new_state}},
        )

    def _open_store(self, aws_client, infrastructure):
        self._change_store_state(aws_client, infrastructure, True)

    def _close_store(self, aws_client, infrastructure):
        self._change_store_state(aws_client, infrastructure, False)

    def _clean_table(self, aws_client, table_name: str):
        items = aws_client.dynamodb.scan(TableName=table_name, ConsistentRead=True)["Items"]
        for item in items:
            aws_client.dynamodb.delete_item(TableName=table_name, Key={"PK": item["PK"]})

    def _stop_any_running_executions(self, aws_client, state_machine_arn: str):
        running_executions = aws_client.stepfunctions.list_executions(
            stateMachineArn=state_machine_arn, statusFilter="RUNNING"
        )
        for re in running_executions["executions"]:
            aws_client.stepfunctions.stop_execution(executionArn=re["executionArn"])
            await_execution_terminated(aws_client.stepfunctions, re["executionArn"])

    def _clear_log_group(self, aws_client, log_group_name: str):
        for ls in aws_client.logs.describe_log_streams(logGroupName=log_group_name)["logStreams"]:
            retry(
                lambda: aws_client.logs.delete_log_stream(
                    logGroupName=log_group_name, logStreamName=ls["logStreamName"]
                )
            )

    def _get_execution_with_prefix(
        self, aws_client, state_machine_arn: str, execution_id_prefix: str
    ) -> str:
        executions = (
            aws_client.stepfunctions.get_paginator("list_executions")
            .paginate(stateMachineArn=state_machine_arn)
            .build_full_result()
        )["executions"]
        matched_executions = [e for e in executions if e["name"].startswith(execution_id_prefix)]
        return matched_executions[0]["executionArn"]

    def _filter_log_events_for_execution_id(
        self, aws_client, log_group_name: str, execution_arn: str, detail_type: str
    ):
        log_events = aws_client.logs.filter_log_events(
            logGroupName=log_group_name,
            filterPattern=f'{{ ($.detail-type = "{detail_type}") && (($.resources[1] = "{execution_arn}") || ($.resources[0] = "{execution_arn}"))}}',  # TODO: is it guaranteed that the second one is the execution_arn?
        )["events"]
        return log_events

    @markers.aws.unknown
    def test_deployed_infra_state(self, aws_client, infrastructure, snapshot):
        outputs = infrastructure.get_stack_outputs(stack_name=STACK_NAME)
        snapshot.match("outputs", outputs)
        describe_stack = aws_client.cloudformation.describe_stacks(StackName=STACK_NAME)["Stacks"][
            0
        ]
        snapshot.match("describe_stack", describe_stack)
        describe_stack_resources = aws_client.cloudformation.describe_stack_resources(
            StackName=STACK_NAME
        )
        snapshot.match("describe_stack_resources", describe_stack_resources)

        # collect service level describe calls
        service_resources = {}
        for stack_resource in describe_stack_resources["StackResources"]:
            match stack_resource["ResourceType"]:
                case "AWS::Lambda::Function":
                    service_resources[
                        stack_resource["LogicalResourceId"]
                    ] = aws_client.lambda_.get_function(
                        FunctionName=stack_resource["PhysicalResourceId"]
                    )
                case "AWS::StepFunctions::StateMachine":
                    service_resources[
                        stack_resource["LogicalResourceId"]
                    ] = aws_client.stepfunctions.describe_state_machine(
                        stateMachineArn=stack_resource["PhysicalResourceId"]
                    )
                case "AWS::DynamoDB::Table":
                    service_resources[
                        stack_resource["LogicalResourceId"]
                    ] = aws_client.dynamodb.describe_table(
                        TableName=stack_resource["PhysicalResourceId"]
                    )
        snapshot.match("resources", service_resources)

    @markers.aws.validated
    def test_populate_data(self, aws_client, infrastructure: "InfraProvisioner"):
        """populate dynamodb table with data"""
        outputs = infrastructure.get_stack_outputs(stack_name=STACK_NAME)

        config_table_name = outputs["ConfigTableName"]
        counting_table_name = outputs["CountingTableName"]
        populate_data_fn = outputs["PopulateDbFunctionName"]

        self._clean_table(aws_client, config_table_name)
        self._clean_table(aws_client, counting_table_name)

        objs = aws_client.dynamodb.scan(TableName=config_table_name)
        assert objs["Count"] == 0
        objs = aws_client.dynamodb.scan(TableName=counting_table_name)
        assert objs["Count"] == 0

        # populate the data now (sync)
        result = aws_client.lambda_.invoke(
            FunctionName=populate_data_fn, InvocationType="RequestResponse", LogType="Tail"
        )
        logs = to_str(base64.b64decode(result["LogResult"]))

        config_table_name = outputs["ConfigTableName"]
        counting_table_name = outputs["CountingTableName"]
        objs = aws_client.dynamodb.scan(TableName=config_table_name, ConsistentRead=True)
        assert objs["Count"] > 0
        objs = aws_client.dynamodb.scan(TableName=counting_table_name, ConsistentRead=True)
        assert objs["Count"] > 0

    @markers.aws.validated
    def test_open_store(self, aws_client, infrastructure):
        """
        https://workshop.serverlesscoffee.com/1b-workflow/8-testing.html
        """
        outputs = infrastructure.get_stack_outputs(stack_name=STACK_NAME)
        sm_arn = outputs["OrderProcessorWorkflowStateMachineArn"]
        self._open_store(aws_client, infrastructure)

        payload = {"detail": {"orderId": "1", "userId": "testuser"}}
        execution_name = f"test-order-flow-shop-open-{short_uid()}"
        execution_arn = aws_client.stepfunctions.start_execution(
            stateMachineArn=sm_arn, name=execution_name, input=json.dumps(payload)
        )["executionArn"]
        await_execution_terminated(aws_client.stepfunctions, execution_arn)
        describe_execution = aws_client.stepfunctions.describe_execution(executionArn=execution_arn)
        assert describe_execution["status"] == "SUCCEEDED"

    @markers.aws.validated
    def test_closed_store_aborts(self, aws_client, infrastructure):
        """
        https://workshop.serverlesscoffee.com/1b-workflow/8-testing.html
        """
        outputs = infrastructure.get_stack_outputs(stack_name=STACK_NAME)
        sm_arn = outputs["OrderProcessorWorkflowStateMachineArn"]
        self._close_store(aws_client, infrastructure)
        payload = {"detail": {"orderId": "1", "userId": "testuser"}}
        execution_name = f"test-order-flow-shop-closed-{short_uid()}"
        execution = aws_client.stepfunctions.start_execution(
            stateMachineArn=sm_arn, name=execution_name, input=json.dumps(payload)
        )
        execution_arn = execution["executionArn"]
        await_execution_terminated(aws_client.stepfunctions, execution_arn)
        describe_execution = aws_client.stepfunctions.describe_execution(executionArn=execution_arn)
        assert describe_execution["status"] == "SUCCEEDED"

    @markers.aws.validated
    def test_mocking_of_validator_service(self, aws_client, infrastructure):
        """
        The validator service would normally trigger this,
        but here we mock it by manually passing an event which will trigger the OrderProcessorWorkflow state machine
        """
        outputs = infrastructure.get_stack_outputs(stack_name=STACK_NAME)
        sm_arn = outputs["OrderProcessorWorkflowStateMachineArn"]
        event_bus_name = outputs["EventBusName"]
        config_table_name = outputs["ConfigTableName"]

        self._close_store(aws_client, infrastructure)

        # mock validator service (scan of QR code => new order)
        put_events = aws_client.events.put_events(
            Entries=[
                {
                    "Source": SERVERLESSPRESSO_SOURCE,
                    "DetailType": "Validator.NewOrder",
                    "Detail": json.dumps({"userId": 1, "orderId": 1}),
                    "EventBusName": event_bus_name,
                }
            ]
        )

        # there should now be an execution with the message id
        # executions = aws_client.stepfunctions.list_executions(stateMachineArn=sm_arn)
        executions = (
            aws_client.stepfunctions.get_paginator("list_executions")
            .paginate(stateMachineArn=sm_arn)
            .build_full_result()
        )

        def _check_execution():
            matched_executions = [
                e
                for e in executions["executions"]
                if e["name"].startswith(put_events["Entries"][0]["EventId"])
            ]
            assert matched_executions
            execution_arn = matched_executions[0]["executionArn"]
            await_execution_terminated(aws_client.stepfunctions, execution_arn)

        retry(_check_execution, sleep=2, retries=10, sleep_before=5)

    @markers.aws.validated
    def test_workflow_start(self, aws_client, infrastructure):
        outputs = infrastructure.get_stack_outputs(stack_name=STACK_NAME)
        order_manager_sm_arn = outputs["OrderManagerStateMachineArn"]
        event_bus_name = outputs["EventBusName"]
        self._open_store(aws_client, infrastructure)

        # mock validator service (scan of QR code => new order)
        put_events = aws_client.events.put_events(
            Entries=[
                {
                    "Source": SERVERLESSPRESSO_SOURCE,
                    "DetailType": "Validator.NewOrder",
                    "Detail": json.dumps({"userId": "1", "orderId": "3"}),
                    "EventBusName": event_bus_name,
                }
            ]
        )
        time.sleep(10)  # TODO: wait for order to arrive in dynamodb

        payload = {
            "action": "",
            "body": {
                "userId": "1",
                "drink": "Cappuccino",
                "modifiers": [],
                "icon": "barista-icons_cappuccino-alternative",
            },
            "orderId": "3",
            "baristaUserId": "3",
        }
        aws_client.stepfunctions.start_execution(
            stateMachineArn=order_manager_sm_arn, input=json.dumps(payload)
        )

        # TODO: wait for order to arrive for barista to confirm

        # # claim
        # payload_barista_make = {"action": "make", "body": {}, "orderId": "2", "baristaUserId": "3"}
        # aws_client.stepfunctions.start_execution(
        #     stateMachineArn=order_manager_sm_arn, input=json.dumps(payload_barista_make)
        # )
        #
        # # complete
        # payload_barista_complete = {
        #     "action": "complete",
        #     "body": {
        #         "userId": "1",
        #         "drink": "Cappuccino",
        #         "modifiers": [],
        #         "icon": "barista-icons_cappuccino-alternative",
        #     },
        #     "orderId": "2",
        #     "baristaUserId": "3",
        # }
        # aws_client.stepfunctions.start_execution(
        #     stateMachineArn=order_manager_sm_arn, input=json.dumps(payload_barista_complete)
        # )
        #
        # # check order state in dynamodb
        # order_state = _get_order_state(aws_client, infrastructure, order_id="2")
        # assert order_state["state"] == "COMPLETED"

    @markers.aws.validated
    def test_concurrent_order_limit(self, aws_client, infrastructure):
        outputs = infrastructure.get_stack_outputs(stack_name=STACK_NAME)
        order_manager_sm_arn = outputs["OrderManagerStateMachineArn"]
        order_processor_sm_arn = outputs["OrderProcessorWorkflowStateMachineArn"]
        event_bus_name = outputs["EventBusName"]
        log_group_name = outputs["LogAllLogGroupName"]

        self._stop_any_running_executions(aws_client, order_processor_sm_arn)
        self._stop_any_running_executions(aws_client, order_manager_sm_arn)
        self._clear_log_group(aws_client, log_group_name)
        self._open_store(aws_client, infrastructure)

        # start 5 orders
        initial_events = [
            {
                "Source": SERVERLESSPRESSO_SOURCE,
                "DetailType": "Validator.NewOrder",
                "Detail": json.dumps({"userId": "1", "orderId": f"concurrent-limit-test-{i}"}),
                "EventBusName": event_bus_name,
            }
            for i in range(5)
        ]
        aws_client.events.put_events(Entries=initial_events)
        # all 5 should be in "Emit - Workflow Started TT" state waiting for the callback with a token
        time.sleep(2)  # TODO: instead just check if all 5 are in a waiting state

        # 6th order should fail at "Is capacity available?" state
        put_events = aws_client.events.put_events(
            Entries=[
                {
                    "Source": SERVERLESSPRESSO_SOURCE,
                    "DetailType": "Validator.NewOrder",
                    "Detail": json.dumps({"userId": "1", "orderId": f"concurrent-limit-test-FAIL"}),
                    "EventBusName": event_bus_name,
                }
            ]
        )

        # wait for started execution
        execution_id_prefix = put_events["Entries"][0]["EventId"]
        execution_arn = retry(
            lambda: self._get_execution_with_prefix(
                aws_client,
                state_machine_arn=order_processor_sm_arn,
                execution_id_prefix=execution_id_prefix,
            ),
            sleep=2,
            retries=10,
            sleep_before=2,
        )
        await_execution_terminated(aws_client.stepfunctions, execution_arn)
        finished_execution = aws_client.stepfunctions.describe_execution(executionArn=execution_arn)
        assert finished_execution["status"] == "SUCCEEDED"

        # verify that event "OrderProcessor.ShopUnavailable" was emitted for the last execution
        def _check_event_exists():
            assert self._filter_log_events_for_execution_id(
                aws_client,
                log_group_name=log_group_name,
                execution_arn=execution_arn,
                detail_type="OrderProcessor.ShopUnavailable",
            )

        retry(_check_event_exists, sleep=2, retries=10, sleep_before=2)

    def test_customer_timeout(self):
        # TODO
        ...

    def test_barista_timeout(self):
        # TODO
        ...

    def test_full_e2e(self):
        # TODO: do a full test via HTTP requests through apigw, cognito, etc.
        ...

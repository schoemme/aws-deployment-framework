# Copyright Amazon.com Inc. or its affiliates.
# SPDX-License-Identifier: MIT-0

"""CloudFormation module used throughout the ADF
"""

import random
import re
import os

from botocore.exceptions import WaiterError, ClientError, ValidationError
from botocore.config import Config
import tenacity

# ADF imports
from errors import InvalidTemplateError, GenericAccountConfigureError
from logger import configure_logger
from paginator import paginator

LOGGER = configure_logger(__name__)
STACK_TERMINATION_PROTECTION = os.environ.get('TERMINATION_PROTECTION', False)
CFN_CONFIG = Config(
    retries={
        "max_attempts": 10,
    },
)
# A stack name can contain only alphanumeric characters (case sensitive)
# and hyphens.
CFN_UNACCEPTED_CHARS = re.compile(r"[^-a-zA-Z0-9]")
ADF_GLOBAL_IAM_STACK_NAME = 'adf-global-base-iam'
ADF_GLOBAL_BOOTSTRAP_STACK_NAME = 'adf-global-base-bootstrap'
ADF_GLOBAL_ADF_BUILD_STACK_NAME = 'adf-global-base-adf-build'


class StackProperties:
    clean_stack_status = [
        'CREATE_FAILED',
        'CREATE_COMPLETE',
        'ROLLBACK_FAILED',
        'ROLLBACK_COMPLETE',
        'DELETE_FAILED',
        'UPDATE_IN_PROGRESS',
        'UPDATE_COMPLETE_CLEANUP_IN_PROGRESS',
        'UPDATE_COMPLETE',
        'UPDATE_ROLLBACK_IN_PROGRESS',
        'UPDATE_ROLLBACK_FAILED',
        'UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS',
        'UPDATE_ROLLBACK_COMPLETE',
        'REVIEW_IN_PROGRESS'
    ]
    clean_before_create_update_states = [
        'CREATE_FAILED',
        'ROLLBACK_FAILED',
        'ROLLBACK_COMPLETE',
        'DELETE_FAILED',
        'REVIEW_IN_PROGRESS',
    ]
    in_progress_state_waiters = {
        'UPDATE_IN_PROGRESS': 'stack_update_complete',
        'CREATE_IN_PROGRESS': 'stack_create_complete',
        'UPDATE_ROLLBACK_IN_PROGRESS': 'stack_rollback_complete',
        'UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS': (
            'stack_rollback_complete'
        ),
        'DELETE_IN_PROGRESS': 'stack_delete_complete',
        'REVIEW_IN_PROGRESS': 'change_set_create_complete',
    }
    all_except_deleted_states = [
        'CREATE_IN_PROGRESS',
        'CREATE_FAILED',
        'CREATE_COMPLETE',
        'ROLLBACK_IN_PROGRESS',
        'ROLLBACK_FAILED',
        'ROLLBACK_COMPLETE',
        'DELETE_IN_PROGRESS',
        'DELETE_FAILED',
        'UPDATE_IN_PROGRESS',
        'UPDATE_COMPLETE_CLEANUP_IN_PROGRESS',
        'UPDATE_COMPLETE',
        'UPDATE_FAILED',
        'UPDATE_ROLLBACK_IN_PROGRESS',
        'UPDATE_ROLLBACK_FAILED',
        'UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS',
        'UPDATE_ROLLBACK_COMPLETE',
        'REVIEW_IN_PROGRESS',
        'IMPORT_IN_PROGRESS',
        'IMPORT_COMPLETE',
        'IMPORT_ROLLBACK_IN_PROGRESS',
        'IMPORT_ROLLBACK_FAILED',
        'IMPORT_ROLLBACK_COMPLETE',
    ]

    def __init__(
            self,
            region,
            deployment_account_region,
            stack_name,
            s3_key_path=None,
            s3=None,
    ):
        self.region = region
        self.deployment_account_region = deployment_account_region
        self.s3_key_path = s3_key_path
        self.ou_name = (
            self.s3_key_path.split('/')[-1] if self.s3_key_path
            else None
        )
        self.s3 = s3
        self.stack_name = stack_name or self._get_stack_name()

    def _get_geo_prefix(self):
        return (
            'global'
            if self.region == self.deployment_account_region
            else 'regional'
        )

    def _create_template_path(self, path, filename_override=None):
        return f'{path}/{filename_override or self._get_geo_prefix()}.yml'

    def _create_parameter_path(self, path):
        return f'{path}/{self._get_geo_prefix()}-params.json'

    def get_template_url(self):
        return self.s3.fetch_s3_url(
            self._create_template_path(self.s3_key_path)
        )

    def get_parameters(self):
        try:
            key = self.s3.fetch_s3_url(
                self._create_parameter_path(self.s3_key_path)
            )
            return self.s3.read_object(key) if key else []
        except ClientError:
            return []

    def _get_stack_name(self):
        stack_suffix = (
            self.ou_name if self.ou_name in ['deployment', 'adf-build']
            else 'bootstrap'
        )
        raw_stack_name = f'adf-{self._get_geo_prefix()}-base-{stack_suffix}'
        return CFN_UNACCEPTED_CHARS.sub("-", raw_stack_name)

    def _get_valid_stack_names(self):
        valid_stack_names = [self._get_stack_name()]
        if self.region == self.deployment_account_region:
            valid_stack_names.append(ADF_GLOBAL_IAM_STACK_NAME)
            valid_stack_names.append(ADF_GLOBAL_BOOTSTRAP_STACK_NAME)
            valid_stack_names.append(ADF_GLOBAL_ADF_BUILD_STACK_NAME)

        return valid_stack_names


class WaitException(Exception):
    pass


class CloudFormation(StackProperties):
    # pylint: disable=too-many-arguments
    def __init__(
            self,
            region,
            deployment_account_region,
            role,
            template_url=None,
            wait=False,
            stack_name=None,
            s3=None,
            s3_key_path=None,
            parameters=None,
            account_id=None,  # Used for logging visibility
            role_arn=None,
    ):
        self.client = role.client(
            'cloudformation',
            region_name=region,
            config=CFN_CONFIG,
        )
        self.wait = wait
        self.parameters = parameters
        self.account_id = account_id
        self.template_url = template_url
        self.role_arn = role_arn
        StackProperties.__init__(
            self,
            region=region,
            deployment_account_region=deployment_account_region,
            stack_name=stack_name,
            s3=s3,
            s3_key_path=s3_key_path
        )

    def validate_template(self):
        try:
            return self.client.validate_template(TemplateURL=self.template_url)
        except ClientError as error:
            LOGGER.error(
                "%s in %s - Template validation of %s failed, see %s",
                self.account_id,
                self.region,
                self.stack_name,
                self.template_url,
            )
            raise InvalidTemplateError(
                f"{self.template_url}: {error}",
            ) from None

    def _wait_if_in_progress(self):
        status = self.get_stack_status()
        if status not in StackProperties.in_progress_state_waiters:
            return

        waiter_type = StackProperties.in_progress_state_waiters[status]
        if 'change_set' in waiter_type:
            self._wait_change_set()
            return

        self._wait_stack(waiter_type, self.stack_name)

    def _wait_stack(self, waiter_type, stack_name):
        try:
            waiter = self.client.get_waiter(waiter_type)
            LOGGER.info(
                '%s in %s - Waiting for CloudFormation stack: %s to reach %s',
                self.account_id,
                self.region,
                stack_name,
                waiter_type,
            )
            waiter.wait(
                StackName=stack_name,
                WaiterConfig={
                    'Delay': CloudFormation._random_delay(),
                    'MaxAttempts': 45
                }
            )
        except (WaiterError, ClientError) as client_error:
            LOGGER.error(
                "%s in %s - Failed to wait for stack %s error %s",
                self.account_id,
                self.region,
                self.stack_name,
                client_error,
            )
            raise

    def _wait_change_set(self):
        try:
            waiter = self.client.get_waiter('change_set_create_complete')

            LOGGER.debug(
                '%s in %s - Waiting for CloudFormation Change Set to '
                'complete creation: %s',
                self.account_id,
                self.region,
                self.stack_name,
            )

            waiter.wait(
                StackName=self.stack_name,
                ChangeSetName=self.stack_name,
                WaiterConfig={
                    'Delay': CloudFormation._random_delay(),
                    'MaxAttempts': 20
                }
            )
        except (WaiterError, ClientError) as error:
            if not CloudFormation._change_set_failed_due_to_empty(
                error.last_response["Status"],
                error.last_response["StatusReason"],
            ):
                LOGGER.error(
                    "%s in %s - Failed to wait for change set of %s error %s",
                    self.account_id,
                    self.region,
                    self.stack_name,
                    error,
                )
            raise

    def _get_waiter_type(self):
        if self._get_change_set_type() == 'UPDATE':
            return 'stack_update_complete'
        return 'stack_create_complete'

    def _get_change_set_type(self):
        status = self.get_stack_status()
        if (
            # Stack does not exists, needs to be created:
            status is None
            # Or stack needs to be recreated:
            or status in StackProperties.clean_before_create_update_states
        ):
            return 'CREATE'
        return 'UPDATE'

    def _describe_change_set(self):
        try:
            return self.client.describe_change_set(
                ChangeSetName=self.stack_name,
                StackName=self.stack_name
            )
        except ClientError:
            return False

    def _clean_up_when_required(self):
        stack_status = self.get_stack_status()
        if not stack_status:
            # No stack found, we can continue as planned
            return

        if stack_status in StackProperties.clean_before_create_update_states:
            LOGGER.info(
                '%s in %s - CloudFormation Stack %s is in %s, which requires '
                'clean up before we can modify it. Deleting stack...',
                self.account_id,
                self.region,
                self.stack_name,
                stack_status,
            )
            self.delete_stack(stack_name=self.stack_name, wait_override=True)
            # If we deleted the stack, there is no need to delete change sets
            return

        change_set_state = self._describe_change_set()
        if change_set_state:
            LOGGER.info(
                '%s in %s - CloudFormation Change set on %s named %s already '
                'exists, deleting change set...',
                self.account_id,
                self.region,
                self.stack_name,
                self.stack_name,
            )
            self._delete_change_set()
            self._wait_until_change_set_is_deleted()

    @tenacity.retry(
        retry=tenacity.retry_if_exception_type(WaitException),
        stop=tenacity.stop_after_attempt(20),
        wait=tenacity.wait_random_exponential(),
    )
    def _wait_until_change_set_is_deleted(self):
        change_set_state = self._describe_change_set()
        if change_set_state:
            # We still found a change set, throwing exception
            # so we can retry until it is no longer present
            raise WaitException()

    def _create_change_set(self):
        """
        Creates a CloudFormation change set from a template
        """
        LOGGER.debug(
            "%s in %s - CloudFormation calling _create_change_set for %s",
            self.account_id,
            self.region,
            self.stack_name,
        )
        try:
            self.template_url = (
                self.template_url
                if self.template_url is not None
                else self.get_template_url()
            )
            if self.template_url:
                self.validate_template()
                change_set_params = {
                    "StackName": self.stack_name,
                    "TemplateURL": self.template_url,
                    "Parameters": (
                        self.parameters
                        if self.parameters is not None
                        else self.get_parameters()
                    ),
                    "Capabilities": [
                        "CAPABILITY_NAMED_IAM",
                        "CAPABILITY_AUTO_EXPAND",
                    ],
                    "Tags": [{
                        'Key': 'createdBy',
                        'Value': 'ADF'
                    }],
                    "ChangeSetName": self.stack_name,
                    "ChangeSetType": self._get_change_set_type()
                }
                if self.role_arn:
                    change_set_params["RoleARN"] = self.role_arn
                self._clean_up_when_required()
                self.client.create_change_set(**change_set_params)
                self._wait_change_set()
                return True
            return False
        except ClientError as error:
            LOGGER.error(
                "%s in %s - Failed to create the change set for %s",
                self.account_id,
                self.region,
                self.stack_name,
                exc_info=1,
            )
            self._delete_change_set()
            raise GenericAccountConfigureError(error) from error
        except WaiterError as error:
            err = error.last_response
            if CloudFormation._change_set_failed_due_to_empty(
                err["Status"],
                err["StatusReason"],
            ):
                LOGGER.debug(
                    "%s in %s - CloudFormation ChangeSet %s does not contain "
                    "changes",
                    self.account_id,
                    self.region,
                    self.stack_name,
                )
                self._delete_change_set()
                return False

            LOGGER.error(
                "%s in %s - CloudFormation stack %s create change set error: "
                "%s",
                self.account_id,
                self.region,
                self.stack_name,
                err["StatusReason"],
                exc_info=1,
            )
            self._delete_change_set()
            raise

    @staticmethod
    def _change_set_failed_due_to_empty(status, reason):
        return (
            status == "FAILED"
            and (
                "The submitted information didn't contain changes." in reason
                or "No updates are to be performed" in reason
            )
        )

    def _update_stack_termination_protection(self):
        try:
            termination_protection = STACK_TERMINATION_PROTECTION == "True"
            self.client.update_termination_protection(
                EnableTerminationProtection=termination_protection,
                StackName=self.stack_name,
            )
        except ClientError as error:
            LOGGER.error(
                '%s in %s - CloudFormation Stack %s, update stack termination '
                'protection error: %s',
                self.account_id,
                self.region,
                self.stack_name,
                error,
            )

    def _delete_change_set(self):
        try:
            self.client.delete_change_set(
                ChangeSetName=self.stack_name,
                StackName=self.stack_name
            )
        except ClientError as client_error:
            LOGGER.info(
                '%s in %s | CloudFormation stack %s delete change set error: '
                '%s',
                self.account_id,
                self.region,
                self.stack_name,
                client_error,
            )

    def _execute_change_set(self, waiter):
        LOGGER.info(
            '%s in %s - Executing CloudFormation Change Set with name: %s',
            self.account_id,
            self.region,
            self.stack_name,
        )

        self.client.execute_change_set(
            ChangeSetName=self.stack_name,
            StackName=self.stack_name,
        )
        if self.wait:
            self._wait_stack(waiter, self.stack_name)

    def create_iam_stack(self):
        try:
            self.template_url = self.s3.fetch_s3_url(
                self._create_template_path(self.s3_key_path, 'global-iam')
            )
            self.stack_name = ADF_GLOBAL_IAM_STACK_NAME
            self._wait_if_in_progress()
            waiter = self._get_waiter_type()
            create_change_set = self._create_change_set()
            if create_change_set:
                self._execute_change_set(waiter)
                self._update_stack_termination_protection()
        except ClientError as client_error:
            LOGGER.error(
                '%s in %s | CloudFormation stack %s create_iam_stack error: '
                '%s',
                self.account_id,
                self.region,
                self.stack_name,
                client_error,
            )
            raise

    def create_stack(self):
        try:
            self._wait_if_in_progress()
            waiter = self._get_waiter_type()
            create_change_set = self._create_change_set()
            if create_change_set:
                self._execute_change_set(waiter)
                self._update_stack_termination_protection()
        except ClientError as client_error:
            LOGGER.error(
                '%s in %s | CloudFormation stack %s create_stack error: '
                '%s',
                self.account_id,
                self.region,
                self.stack_name,
                client_error,
            )
            raise

    def get_stack_regional_outputs(self):
        return {
            "kms_arn":
                self.get_stack_output("DeploymentFrameworkRegionalKMSKey"),
            "s3_regional_bucket":
                self.get_stack_output("DeploymentFrameworkRegionalS3Bucket"),
        }

    def delete_all_base_stacks(self, wait_override=False):
        self._delete_base_stacks(
            wait_override=wait_override,
        )

    def delete_deprecated_base_stacks(self):
        self._delete_base_stacks(
            wait_override=True,
            deprecated_only=True,
        )

    def _delete_base_stacks(
        self,
        wait_override=False,
        deprecated_only=False,
    ):
        deleted_any = False
        bootstrap_stack_found = False
        for stack in paginator(
            self.client.list_stacks,
            StackStatusFilter=StackProperties.all_except_deleted_states,
        ):
            matches_search = bool(
                re.search(
                    'adf-(global|regional)-base',
                    stack.get('StackName'),
                )
            )
            if not matches_search:
                continue
            if stack.get('ParentId', ''):
                # Skip nested stacks
                continue

            if deleted_any and stack.get('StackName') == ADF_GLOBAL_IAM_STACK_NAME:
                # We deleted the IAM stack already
                continue

            should_be_deleted = (
                not deprecated_only
                or stack.get('StackName') not in self._get_valid_stack_names()
            )
            if not should_be_deleted:
                if stack.get('StackName') != ADF_GLOBAL_IAM_STACK_NAME:
                    bootstrap_stack_found = True
                continue

            if stack.get('StackStatus') == 'DELETE_COMPLETE':
                # Nothing to do here
                continue

            LOGGER.debug(
                'Base stack should be deleted: %s',
                stack.get('StackName'),
            )

            should_delete_iam_stack = (
                not deleted_any
                and self.region == self.deployment_account_region
                and stack.get('StackName') != ADF_GLOBAL_IAM_STACK_NAME
            )
            if should_delete_iam_stack:
                # Remove the IAM stack before deleting an ADF global stack
                # If we are deleting a bootstrap stack, we need to assume this
                # might hosts the roles that get policies attached by the
                # global-iam stack. Since the policies need to be deleted
                # before one can delete the role, we need to delete the global
                # IAM stack first.
                self._delete_iam_stack_if_exists()

            self._delete_stack_or_instruct_user(
                stack_name=stack.get('StackName'),
                stack_status=stack.get('StackStatus'),
                wait_override=wait_override,
            )
            deleted_any = True

        if deprecated_only and not bootstrap_stack_found and not deleted_any:
            # If we did not find any bootstrap stack but we did run into the
            # global IAM stack, then we should delete the global IAM stack.
            # As the policies that the CloudFormation stack manages would
            # need to be recreated and applied to new IAM Roles as created
            # by a upcoming bootstrap stack.
            self._delete_iam_stack_if_exists()

    def _get_stack_status(self, name):
        try:
            LOGGER.debug(
                "%s in %s - Retrieve stack status of: %s",
                self.account_id,
                self.region,
                name,
            )
            response = self.client.describe_stacks(
                StackName=name,
            )
            if response and response.get('Stacks', []):
                return response['Stacks'][0]['StackStatus']
            return None
        except (ClientError, ValidationError) as error:
            if error.response['Error']['Code'] == 'ValidationError':
                LOGGER.debug(
                    "%s in %s - Stack does not exist: %s",
                    self.account_id,
                    self.region,
                    name,
                )
                # If the stack does not exist, a ValidationError is raised.
                return None  # None implies missing
            LOGGER.error(
                "%s in %s - Retrieve stack status of: %s failed (%s): %s",
                self.account_id,
                self.region,
                name,
                error.response['Error']['Code'],
                error.response['Error']['Message'],
            )
            raise

    def _delete_iam_stack_if_exists(self):
        iam_stack_status = self._get_stack_status(ADF_GLOBAL_IAM_STACK_NAME)
        if iam_stack_status:
            self._delete_stack_or_instruct_user(
                stack_name=ADF_GLOBAL_IAM_STACK_NAME,
                stack_status=iam_stack_status,
                wait_override=True,
            )

    def _delete_stack_or_instruct_user(
        self,
        stack_name,
        stack_status,
        wait_override,
    ):
        clean_stack_status = (
            stack_status in StackProperties.clean_stack_status
        )
        if clean_stack_status:
            LOGGER.warning('Removing stack: %s', stack_name)
            self.delete_stack(stack_name, wait_override)
            return

        LOGGER.warning(
            'Please remove stack %s manually, state %s implies that it '
            'cannot be deleted automatically',
            stack_name,
            stack_status,
        )

    def get_stack_output(self, value):
        try:
            LOGGER.debug("Retrieving value: %s", value)
            response = self.client.describe_stacks(
                StackName=self.stack_name
            )
            return [item.get('OutputValue') for item in response.get('Stacks')
                    [0].get('Outputs') if item.get('OutputKey') == value][0]
        except BaseException:  # pylint: disable=broad-exception-caught
            LOGGER.warning(
                "%s in %s - Attempted to get stack output from %s "
                "but it failed.",
                self.account_id,
                self.region,
                self.stack_name,
            )
            return None  # Return None if describe stack call fails

    def get_stack_status(self):
        try:
            stack = self.client.describe_stacks(
                StackName=self.stack_name
            )
            return stack['Stacks'][0]['StackStatus']
        except BaseException as error:
            LOGGER.debug(
                "%s in %s - Attempted to get stack status from %s but it "
                "failed with: %s",
                self.account_id,
                self.region,
                self.stack_name,
                error,
            )
            return None  # Return None if the stack does not exist

    def delete_stack(self, stack_name, wait_override=False):
        try:
            LOGGER.debug(
                '%s in %s - Attempted to delete stack: %s',
                self.account_id,
                self.region,
                stack_name,
            )
            self.client.delete_stack(
                StackName=stack_name,
            )
            if self.wait or wait_override:
                self._wait_stack('stack_delete_complete', stack_name)
        except ClientError as client_error:
            LOGGER.error(
                "%s in %s - Failed to delete stack %s error %s",
                self.account_id,
                self.region,
                self.stack_name,
                client_error,
            )
            raise

    @staticmethod
    def _random_delay():
        return random.randint(11, 49)

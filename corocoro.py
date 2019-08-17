import fire
import boto3
import timeout_decorator
import time
from botocore.exceptions import ClientError

# Default timeout as seconds
DEREGISTER_TIMEOUT      = 490
REGISTER_TIMEOUT        = 490
SLOW_START_WAIT_TIME    = 60
RESTART_TIMEOUT         = 420


class Corocoro(object):

    def __initialize__(self, instance_ids):
        session = boto3.Session(profile_name='default')
        self.__instance_ids = instance_ids
        self.__ec2 = session.client('ec2')
        self.__elbv2 = session.client('elbv2')
        self.__elb = session.client('elb')
        self.__ssm = session.client('ssm')

    # Find target group storing instances
    def __find_target_group(self, instance_id):
        target_tgs = []
        all_tg = self.__elbv2.describe_target_groups()['TargetGroups']
        all_target_tgs = [tg_arn['TargetGroupArn'] for tg_arn in all_tg]
        for tg_arn in all_target_tgs:
            state = None
            port = None
            members = self.__elbv2.describe_target_health(TargetGroupArn=tg_arn)['TargetHealthDescriptions']
            for member in members:
                if member['Target']['Id'] == instance_id:
                    state = member['TargetHealth']['State']
                    port = member['Target']['Port']
            if state == 'healthy':
                target_tgs.append({'arn': tg_arn, 'port': port})
        return target_tgs

    # Find clb storing instances
    def __find_clb(self, instance_id):
        all_clbs = self.__elb.describe_load_balancers()['LoadBalancerDescriptions']
        all_clbs_name = [clb['LoadBalancerName'] for clb in all_clbs]
        target_clb_names = []
        for clb_name in all_clbs_name:
            # Sometimes, `describe_instance_health` may return exception even if instance_id was correct.
            # https://groups.google.com/forum/#!topic/ansible-project/jMUUYnf7AgA
            try:
                res = self.__elb.describe_instance_health(LoadBalancerName=clb_name,
                                                          Instances=[{'InstanceId': instance_id}])
            except ClientError as e:
                print(f"[WARNING]{e}")
                continue
            state = res['InstanceStates'][0]['State']
            if state == 'InService':
                target_clb_names.append(clb_name)
        return target_clb_names

    # Deregister instance form target group and clb.
    @timeout_decorator.timeout(DEREGISTER_TIMEOUT, timeout_exception=StopIteration)
    def __deregister_instance(self, instance_id, target_tgs, clb_names):

        # Deregister
        for tg in target_tgs:
            print(f"[INFO]Deregister {instance_id} from {tg['arn']}")
            self.__elbv2.deregister_targets(TargetGroupArn=tg['arn'],
                                            Targets=[{'Id': instance_id, 'Port': tg['port']}])
        for clb_name in clb_names:
            print(f"[INFO]Deregister {instance_id} from {clb_name}")
            self.__elb.deregister_instances_from_load_balancer(LoadBalancerName=clb_name,
                                                               Instances=[{'InstanceId': instance_id}])

        # Wait for unused/OutOfService
        for tg in target_tgs:
            print(f"[INFO]Wait for {instance_id} of {tg['arn']} getting unused.")
            while True:
                res = self.__elbv2.describe_target_health(TargetGroupArn=tg['arn'],
                                                          Targets=[{'Id': instance_id, 'Port': tg['port']}])
                state = res['TargetHealthDescriptions'][0]['TargetHealth']['State']
                if state == 'unused':
                    break
                time.sleep(5)

        for clb_name in clb_names:
            print(f"[INFO]Wait for {instance_id} of {clb_name} getting OutOfService.")
            while True:
                res = self.__elb.describe_instance_health(LoadBalancerName=clb_name,
                                                          Instances=[{'InstanceId': instance_id}])
                state = res['InstanceStates'][0]['State']
                if state == 'OutOfService':
                    break
                time.sleep(5)

    # Register instance to target group and clb.
    @timeout_decorator.timeout(REGISTER_TIMEOUT, timeout_exception=StopIteration)
    def __register_instance(self, instance_id, target_tgs, clb_names):

        # Register
        for tg in target_tgs:
            print(f"[INFO]Register {instance_id} to {tg['arn']}")
            self.__elbv2.register_targets(TargetGroupArn=tg['arn'],
                                          Targets=[{'Id': instance_id, 'Port': tg['port']}])
        for clb_name in clb_names:
            print(f"[INFO]Register {instance_id} to {clb_name}")
            self.__elb.register_instances_with_load_balancer(LoadBalancerName=clb_name,
                                                             Instances=[{'InstanceId': instance_id}])

        # Wait for healthy/InService
        for tg in target_tgs:
            print(f"[INFO]Wait for {instance_id} of {tg['arn']} getting healthy.")
            while True:
                res = self.__elbv2.describe_target_health(TargetGroupArn=tg['arn'],
                                                          Targets=[{'Id': instance_id, 'Port': tg['port']}])
                state = res['TargetHealthDescriptions'][0]['TargetHealth']['State']
                if state == 'healthy':
                    break
                time.sleep(5)

        for clb_name in clb_names:
            print(f"[INFO]Wait for {instance_id} of {clb_name} getting InService.")
            while True:
                res = self.__elb.describe_instance_health(LoadBalancerName=clb_name,
                                                          Instances=[{'InstanceId': instance_id}])
                state = res['InstanceStates'][0]['State']
                if state == 'InService':
                    break
                time.sleep(5)

    # Apploval for target instances
    def __approval_execution(self):
        response = self.__ec2.describe_tags(
            Filters=[
                {
                    'Name': 'resource-id',
                    'Values': self.__instance_ids
                },
                {
                    'Name': 'key',
                    'Values': [
                        'Name'
                    ]
                }
            ]
        )
        instance_names = [tag['Value'] for tag in response['Tags']]
        print(f"[INFO]TargetInstance: {instance_names}")
        print("Confirm? (yes/no):")
        input_test_word = input('>>>  ')
        if input_test_word != 'yes':
            return -1
        return 0

    # Stop and start instance
    @timeout_decorator.timeout(RESTART_TIMEOUT, timeout_exception=StopIteration)
    def __stop_start_instance(self, instance_id):
        # Stop instance and wait for status getting 'stopped'
        self.__ec2.stop_instances(InstanceIds=[instance_id])
        while True:
            # If 'IncludeAllInstances=True' is not set, cannot get stopped instances
            res = self.__ec2.describe_instance_status(InstanceIds=[instance_id], IncludeAllInstances=True)
            if res['InstanceStatuses'][0]['InstanceState']['Name'] == 'stopped':
                break
            time.sleep(5)
        # Start instance and wait for status getting 'running'
        self.__ec2.start_instances(InstanceIds=[instance_id])
        while True:
            res = self.__ec2.describe_instance_status(InstanceIds=[instance_id], IncludeAllInstances=True)
            if res['InstanceStatuses'][0]['InstanceState']['Name'] == 'running':
                break
            time.sleep(5)

    def __rolling_exec(self, func):
        # Deregister and exec runcommand and register them each instances
        for instance_id in self.__instance_ids:
            print(f"[INFO]Begin restart-instance process: {instance_id}")

            # Get all target groups registering target instance
            target_tgs = self.__find_target_group(instance_id)
            print(f"[INFO]TargetGroups storing {instance_id}: {target_tgs}")
            clb_names = self.__find_clb(instance_id)
            print(f"[INFO]CLB storing {instance_id}: {clb_names}")

            # Deregister the instance from target groups
            try:
                self.__deregister_instance(instance_id, target_tgs, clb_names)
            except StopIteration:
                raise print(f"[ERROR]Deregistering instance was timeout with {DEREGISTER_TIMEOUT}. "
                            f"InstanceId: {instance_id}")

            # Stop and start instance
            try:
                print(f"[INFO]Restarting {instance_id}")
                self.__stop_start_instance(instance_id)
            except StopIteration:
                raise print(f"[ERROR]Restart was timeout with {RESTART_TIMEOUT}."
                            f"InstanceId: {instance_id}")

            print(f"[INFO]Wait for cool down time({SLOW_START_WAIT_TIME}sec).")
            time.sleep(SLOW_START_WAIT_TIME)

            # Register the instance to target groups
            try:
                self.__register_instance(instance_id, target_tgs, clb_names)
            except StopIteration:
                raise print(f"[ERROR]Registering instance was timeout with {REGISTER_TIMEOUT}. "
                            f"InstanceId: {instance_id}")

    # This is main method
    def restart(self,  *args):
        self.__initialize__(instance_ids=args)
        if self.__approval_execution() == -1:
            print("[INFO]Execution was canceled.")
            return
        self.__rolling_exec(self.__stop_start_instance)
        print("[INFO]All processes were finished!")


if __name__ == '__main__':
    fire.Fire(Corocoro)

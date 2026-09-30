from aws_cdk import (
    Annotations,
    CfnOutput,
    RemovalPolicy,
    Stack,
    Tags,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_sqs as sqs,
)
from constructs import Construct

# Red Hat's official AMI owner account.
RED_HAT_AMI_OWNER = "309956199498"


class ClcServerStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        ctx = self.node.try_get_context
        env_name = ctx("env_name") or "dev"
        is_production = env_name == "prod"
        server_name = f"clc-genomics-server-{env_name}"
        # clc-genomics-server-dev

        self.vpc = ec2.Vpc.from_lookup(
            self,
            "ClcVpc",
            vpc_id="vpc-044c9d6ddbc0b3fc0",
        )

        subnet = ec2.Subnet.from_subnet_attributes(
            self,
            "ClcSubnet",
            subnet_id=ctx("subnet_id") or "subnet-0d5d6260573383be6",
            availability_zone=ctx("subnet_az") or "us-west-2a",
            route_table_id=ctx("route_table_id") or "rtb-04b860328b4218a87",
        )

        # Override with -c ami_id=ami-xxxx to pin an organization-approved image.
        ami_id = ctx("ami_id")
        machine_image = (
            ec2.MachineImage.generic_linux({self.region: ami_id})
            if ami_id
            else ec2.MachineImage.lookup(
                name="RHEL-9.*_HVM-*-x86_64-*",
                owners=[RED_HAT_AMI_OWNER],
                filters={"architecture": ["x86_64"]},
            )
        )

        security_group = ec2.SecurityGroup(
            self,
            "ClcServerSg",
            vpc=self.vpc,
            description="CLC Genomics Server - HTTPS 8443 from approved private networks only",
            allow_all_outbound=True,
        )
        allowed_cidrs = ctx("allowed_cidrs") or [self.vpc.vpc_cidr_block]
        for cidr in allowed_cidrs:
            security_group.add_ingress_rule(
                ec2.Peer.ipv4(cidr),
                ec2.Port.tcp(8443),
                "CLC Server HTTPS from approved private network",
            )

        # Session Manager without internet access: SSM interface endpoints + S3 gateway endpoint.
        endpoint_sg = ec2.SecurityGroup(
            self,
            "SsmEndpointSg",
            vpc=self.vpc,
            description="SSM VPC endpoints - HTTPS from CLC server",
            allow_all_outbound=False,
        )
        endpoint_sg.add_ingress_rule(
            security_group, ec2.Port.tcp(443), "HTTPS from CLC server"
        )
        endpoints = []
        for name, service in {
            "Ssm": ec2.InterfaceVpcEndpointAwsService.SSM,
            "SsmMessages": ec2.InterfaceVpcEndpointAwsService.SSM_MESSAGES,
            "Ec2Messages": ec2.InterfaceVpcEndpointAwsService.EC2_MESSAGES,
        }.items():
            endpoints.append(
                ec2.InterfaceVpcEndpoint(
                    self,
                    f"{name}Endpoint",
                    vpc=self.vpc,
                    service=service,
                    subnets=ec2.SubnetSelection(subnets=[subnet]),
                    security_groups=[endpoint_sg],
                    private_dns_enabled=True,
                    open=False,
                )
            )
        # Needed to download the SSM agent RPM and for SSM agent updates.
        endpoints.append(
            ec2.GatewayVpcEndpoint(
                self,
                "S3Endpoint",
                vpc=self.vpc,
                service=ec2.GatewayVpcEndpointAwsService.S3,
                subnets=[ec2.SubnetSelection(subnets=[subnet])],
            )
        )

        # RHEL AMIs do not ship with the SSM agent; repos are unreachable without internet, so skip them.
        agent_url = f"https://s3.{self.region}.amazonaws.com/amazon-ssm-{self.region}/latest/linux_amd64/amazon-ssm-agent.rpm"
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            f"for i in $(seq 1 30); do curl -fsSL -o /tmp/amazon-ssm-agent.rpm {agent_url} && break; sleep 10; done",
            "dnf install -y --disablerepo='*' /tmp/amazon-ssm-agent.rpm",
            "systemctl enable --now amazon-ssm-agent",
        )

        role_name = ctx("instance_role_name")
        if role_name:
            role = iam.Role.from_role_name(self, "ClcServerRole", role_name)
        else:
            role = iam.Role(
                self,
                "ClcServerRole",
                assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
                managed_policies=[
                    iam.ManagedPolicy.from_aws_managed_policy_name(
                        "AmazonSSMManagedInstanceCore"
                    )
                ],
            )

        self.instance = ec2.Instance(
            self,
            "ClcServer",
            instance_name=server_name,
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnets=[subnet]),
            instance_type=ec2.InstanceType(ctx("instance_type") or "m6i.2xlarge"),
            machine_image=machine_image,
            security_group=security_group,
            role=role,
            user_data=user_data,
            user_data_causes_replacement=True,
            associate_public_ip_address=False,
            require_imdsv2=True,
            disable_api_termination=is_production,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/sda1",
                    volume=ec2.BlockDeviceVolume.ebs(
                        50,
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                        encrypted=True,
                        delete_on_termination=True,
                    ),
                ),
                ec2.BlockDevice(
                    device_name="/dev/sdf",
                    volume=ec2.BlockDeviceVolume.ebs(
                        int(ctx("data_volume_gib") or 500),
                        volume_type=ec2.EbsDeviceVolumeType.GP3,
                        encrypted=True,
                        delete_on_termination=False,
                    ),
                ),
            ],
        )
        for endpoint in endpoints:
            self.instance.node.add_dependency(endpoint)
        if ami_id:
            Annotations.of(self.instance).acknowledge_warning(
                "CloudFormation-Validate::W9010",
                "AMI ID is intentionally pinned to an organization-approved image via cdk.json context.",
            )
        Tags.of(self.instance).add("Environment", env_name)
        Tags.of(self.instance).add("Application", "clc-genomics-server")

        CfnOutput(self, "InstanceId", value=self.instance.instance_id)
        CfnOutput(self, "PrivateIp", value=self.instance.instance_private_ip)

        sqs.Queue(
            self,
            "DemoQueue",
            removal_policy=RemovalPolicy.DESTROY,
        )
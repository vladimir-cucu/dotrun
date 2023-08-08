#! /usr/bin/env python3

import os
import platform
import re
import platform
import re
import sys
import time
import threading
from importlib import metadata

# Packages
import docker
import dockerpty
from dotenv import dotenv_values
from slugify import slugify

__version__ = metadata.version("dotrun")


class Dotrun:
    def __init__(self):
        self.cwd = os.getcwd()
        self.project_name = slugify(os.path.basename(self.cwd))
        self.project_port = dotenv_values(".env").get("PORT", 8080)
        self.additional_mounts = []
        self.container_home = "/home/ubuntu/"
        self.container_path = f"{self.container_home}{self.project_name}"
        # --network host is only supported on Linux
        self.network_host_mode = (
            sys.platform.startswith("linux")
            and "microsoft" not in platform.platform()
        )
        self._get_docker_client()
        self._check_image_updates()
        self._create_cache_volume()

    def _get_docker_client(self):
        try:
            self.docker_client = docker.from_env()
            self.docker_client.ping()
        except (docker.errors.APIError, docker.errors.DockerException) as e:
            print(e)
            print(
                "Dotrun needs Docker to work, please check"
                " if Docker is installed and running"
            )
            sys.exit(1)

    def _check_image_updates(self):
        try:
            self.docker_client.images.get(
                "canonicalwebteam/dotrun-image:latest"
            )
            # Pull the image in the background
            print("Checking for dotrun image updates...")
            threading.Thread(target=self._pull_image)
        except docker.errors.ImageNotFound:
            print("Getting the dotrun image...")
            self._pull_image()

    def _pull_image(self):
        """Pull the dotrun image (if updated) from Docker Hub"""
        self.docker_client.images.pull(
            repository="canonicalwebteam/dotrun-image", tag="latest"
        )

    def _create_cache_volume(self):
        try:
            self.docker_client.volumes.get("dotrun-cache")
        except docker.errors.NotFound:
            self.docker_client.volumes.create(name="dotrun-cache")

            # We need to fix the volume ownership
            self.docker_client.containers.run(
                "canonicalwebteam/dotrun-image",
                f"chown -R ubuntu:ubuntu {self.container_home}.cache",
                user="root",
                mounts=self._prepare_mounts(),
                remove=True,
            )

    def _prepare_mounts(self):
        mounts = [
            docker.types.Mount(
                target=f"{self.container_home}.cache",
                source="dotrun-cache",
                type="volume",
                read_only=False,
                consistency="delegated",
            ),
            docker.types.Mount(
                target=self.container_path,
                source=self.cwd,
                type="bind",
                read_only=False,
                # See https://tkacz.pro/docker-volumes-cached-vs-delegated/
                consistency="cached",
            ),
        ]
        if self.additional_mounts:
            for mount in self.additional_mounts:
                mounts.append(
                    docker.types.Mount(
                        target=f"{self.container_path}/{mount[1]}",
                        source=f"{mount[0]}",
                        type="bind",
                        read_only=False,
                        consistency="cached",
                    )
                )
        return mounts

    def _get_container_name(self, command=None):
        """
        Return a simple name to easily identify the container on Docker
        """
        name = ["dotrun"]

        if self.project_name:
            name.append(self.project_name)

        if command:
            name.append(slugify(command))

        # Timestamp so we don't get name collisions
        name.append(str(int(time.time())))

        name = "-".join(name)

        # Remove duplicated hyphens
        return re.sub(r"(-)+", r"\1", name)

    def _set_additional_mounts(self, command=None):
        """
        Return a list of additional mounts
        """
        if not command:
            return None

        def get_mount(command, mounts):
            mount_index = command.index("-m")
            mount_string = command[mount_index + 1]
            del command[mount_index]
            if ":" in mount_string:
                mount_parts = mount_string.split(":")
                mounts.append(mount_parts)
                del command[mount_index]

            if "-m" in command:
                mounts = get_mount(command, mounts)

            return mounts

        if "-m" in command:
            mounts = get_mount(command, [])

            self.additional_mounts = mounts
        return None

    def create_container(self, command):
        ports = {self.project_port: self.project_port}
        # Run on the same network mode as the host
        network_mode = None
        if command[1:]:
            first_cmd = command[1:][0]

            # Avoid port conflict when running multiple commands
            if first_cmd not in ["start", "serve", "-m"]:
                print("not serve or start")
                ports = {}

            # Set a different container name to run a specific command
            name = self._get_container_name(first_cmd)
        else:
            name = self._get_container_name()
        if self.network_host_mode:
            # network_mode host is incompatible with ports option
            ports = None
            network_mode = "host"

        self._set_additional_mounts(command)

        return self.docker_client.containers.create(
            image="canonicalwebteam/dotrun-image",
            name=name,
            hostname=name,
            mounts=self._prepare_mounts(),
            working_dir=self.container_path,
            environment=[f"DOTRUN_VERSION={__version__}"],
            stdin_open=True,
            tty=True,
            command=command,
            ports=ports,
            network_mode=network_mode,
        )


def cli():
    dotrun = Dotrun()
    command = ["dotrun"]
    command.extend(sys.argv[1:])

    if command[-1] == "version":
        print(f"dotrun v{__version__}")

    container = dotrun.create_container(command)

    # 1 by default
    status_code = 1

    try:
        dockerpty.start(dotrun.docker_client.api, container.id)
        status_code = container.wait().get("StatusCode", 1)
    finally:
        container.stop()
        container.remove()

    return status_code

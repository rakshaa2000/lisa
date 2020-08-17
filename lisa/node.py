from __future__ import annotations

import pathlib
import random
from typing import Any, Dict, Optional, Type, TypeVar, Union, cast

from lisa.executable import CustomScriptBuilder, LightTool, Tool
from lisa.tool import Echo, Uname
from lisa.util import constants, env
from lisa.util.exceptions import LisaException
from lisa.util.logger import get_logger
from lisa.util.perf_timer import create_timer
from lisa.util.process import ExecutableResult, Process
from lisa.util.shell import ConnectionInfo, LocalShell, Shell, SshShell

T = TypeVar("T")


class Node:
    def __init__(
        self,
        identifier: str,
        is_remote: bool = True,
        spec: Optional[Dict[str, object]] = None,
        is_default: bool = False,
    ) -> None:
        self.is_default = is_default
        self.is_remote = is_remote
        self.spec = spec
        self.name: str = ""

        self.identifier = identifier
        self.shell: Shell = LocalShell()

        self.kernel_release: str = ""
        self.kernel_version: str = ""
        self.hardware_platform: str = ""
        self.operating_system: str = ""
        self.tool = LightTool(self)

        self._connection_info: Optional[ConnectionInfo] = None
        self._working_path: pathlib.PurePath = pathlib.PurePath()

        self._tools: Dict[str, Tool] = dict()

        self._is_initialized: bool = False
        self._is_linux: bool = True
        self._log = get_logger("node", self.identifier)

    @staticmethod
    def create(
        identifier: str,
        spec: Optional[Dict[str, object]] = None,
        node_type: str = constants.ENVIRONMENTS_NODES_REMOTE,
        is_default: bool = False,
    ) -> Node:
        if node_type == constants.ENVIRONMENTS_NODES_REMOTE:
            is_remote = True
        elif node_type == constants.ENVIRONMENTS_NODES_LOCAL:
            is_remote = False
        else:
            raise LisaException(f"unsupported node_type '{node_type}'")
        node = Node(identifier, spec=spec, is_remote=is_remote, is_default=is_default)
        node._log.debug(
            f"created node '{node_type}', isDefault: {is_default}, "
            f"isRemote: {is_remote}"
        )
        return node

    def set_connection_info(
        self,
        address: str = "",
        port: int = 22,
        publicAddress: str = "",
        publicPort: int = 22,
        username: str = "root",
        password: str = "",
        privateKeyFile: str = "",
    ) -> None:
        if self._connection_info is not None:
            raise LisaException(
                "node is set connection information already, cannot set again"
            )

        if not address and not publicAddress:
            raise LisaException(
                "at least one of address and publicAddress need to be set"
            )
        elif not address:
            address = publicAddress
        elif not publicAddress:
            publicAddress = address

        if not port and not publicPort:
            raise LisaException("at least one of port and publicPort need to be set")
        elif not port:
            port = publicPort
        elif not publicPort:
            publicPort = port

        self._connection_info = ConnectionInfo(
            publicAddress, publicPort, username, password, privateKeyFile,
        )
        self.shell = SshShell(self._connection_info)
        self.internal_address = address
        self.internal_port = port

    def get_tool_path(self, tool: Optional[Tool] = None) -> pathlib.PurePath:
        assert self._working_path
        if tool:
            tool_name = tool.name
            tool_path = self._working_path.joinpath(constants.PATH_TOOL, tool_name)
        else:
            tool_path = self._working_path.joinpath(constants.PATH_TOOL)
        return tool_path

    def get_tool(self, tool_type: Union[Type[T], CustomScriptBuilder, str]) -> T:
        if tool_type is CustomScriptBuilder:
            raise LisaException("CustomScript should call getScript with instance")
        if isinstance(tool_type, CustomScriptBuilder):
            tool_key = tool_type.name
        elif isinstance(tool_type, str):
            tool_key = tool_type
        else:
            tool_key = tool_type.__name__.lower()
        tool = self._tools.get(tool_key)
        if tool is None:
            # the Tool is not installed on current node, try to install it.
            tool_log = get_logger("tool", tool_key, self._log)
            tool_log.debug("is initializing")

            if isinstance(tool_type, CustomScriptBuilder):
                tool = tool_type.build(self)
            elif isinstance(tool_type, str):
                raise LisaException(
                    f"{tool_type} cannot be found. "
                    f"lightweight usage need to get_tool with type before using it."
                )
            else:
                cast_tool_type = cast(Type[Tool], tool_type)
                tool = cast_tool_type(self)
                tool.initialize()

            if not tool.is_installed:
                tool_log.debug("not installed")
                if tool.can_install:
                    tool_log.debug("installing")
                    timer = create_timer()
                    is_success = tool.install()
                    tool_log.debug(f"installed in {timer}")
                    if not is_success:
                        raise LisaException("install failed")
                else:
                    raise LisaException(
                        "doesn't support install on "
                        f"Node({self.identifier}), "
                        f"Linux({self.is_linux}), "
                        f"Remote({self.is_remote})"
                    )
            else:
                tool_log.debug("installed already")
            self._tools[tool_key] = tool
        return cast(T, tool)

    def execute(
        self,
        cmd: str,
        shell: bool = False,
        no_error_log: bool = False,
        no_info_log: bool = False,
        cwd: Optional[pathlib.PurePath] = None,
    ) -> ExecutableResult:
        process = self.executeasync(
            cmd,
            shell=shell,
            no_error_log=no_error_log,
            no_info_log=no_info_log,
            cwd=cwd,
        )
        return process.wait_result()

    def executeasync(
        self,
        cmd: str,
        shell: bool = False,
        no_error_log: bool = False,
        no_info_log: bool = False,
        cwd: Optional[pathlib.PurePath] = None,
    ) -> Process:
        self._initialize()
        return self._execute(
            cmd,
            shell=shell,
            no_error_log=no_error_log,
            no_info_log=no_info_log,
            cwd=cwd,
        )

    @property
    def is_linux(self) -> bool:
        self._initialize()
        return self._is_linux

    def _initialize(self) -> None:
        if not self._is_initialized:
            # prevent loop calls, set _isInitialized to True first
            self._is_initialized = True
            self._log.debug(f"initializing node {self.name}")
            self.shell.initialize()
            uname = self.get_tool(Uname)
            (
                self.kernel_release,
                self.kernel_version,
                self.hardware_platform,
                self.operating_system,
            ) = uname.get_linux_information(no_error_log=True)
            if (not self.kernel_release) or ("Linux" not in self.operating_system):
                self._is_linux = False
            if self._is_linux:
                self._log.info(
                    f"initialized Linux node '{self.name}', "
                    f"kernelRelease: {self.kernel_release}, "
                    f"kernelVersion: {self.kernel_version}"
                    f"hardwarePlatform: {self.hardware_platform}"
                )
            else:
                self._log.info(f"initialized Windows node '{self.name}', ")

            # set working path
            if self.is_remote:
                assert self.shell
                assert self._connection_info

                if self.is_linux:
                    remote_root_path = pathlib.Path("$HOME")
                else:
                    remote_root_path = pathlib.Path("%TEMP%")
                working_path = remote_root_path.joinpath(
                    constants.PATH_REMOTE_ROOT, env.get_run_path()
                ).as_posix()

                # expand environment variables in path
                echo = self.get_tool(Echo)
                result = echo.run(working_path, shell=True)

                # PurePath is more reasonable here, but spurplus doesn't support it.
                if self.is_linux:
                    self._working_path = pathlib.PurePosixPath(result.stdout)
                else:
                    self._working_path = pathlib.PureWindowsPath(result.stdout)
            else:
                self._working_path = pathlib.Path(env.get_run_local_path())
            self._log.debug(f"working path is: '{self._working_path}'")
            self.shell.mkdir(self._working_path, parents=True, exist_ok=True)

    def _execute(
        self,
        cmd: str,
        shell: bool = False,
        no_error_log: bool = False,
        no_info_log: bool = False,
        cwd: Optional[pathlib.PurePath] = None,
    ) -> Process:
        cmd_id = str(random.randint(0, 10000))
        process = Process(
            cmd_id, self.shell, parent_logger=self._log, is_linux=self.is_linux
        )
        process.start(
            cmd,
            shell=shell,
            no_error_log=no_error_log,
            no_info_log=no_info_log,
            cwd=cwd,
        )
        return process

    def close(self) -> None:
        self.shell.close()


def from_config(identifier: str, config: Dict[str, object]) -> Optional[Node]:
    node_type = cast(str, config.get(constants.TYPE))
    node = None
    if node_type is None:
        raise LisaException("type of node shouldn't be None")
    if node_type in [
        constants.ENVIRONMENTS_NODES_LOCAL,
        constants.ENVIRONMENTS_NODES_REMOTE,
    ]:
        is_default = cast(bool, config.get(constants.IS_DEFAULT, False))
        node = Node.create(identifier, node_type=node_type, is_default=is_default)
        if node.is_remote:
            fields = [
                constants.ENVIRONMENTS_NODES_REMOTE_ADDRESS,
                constants.ENVIRONMENTS_NODES_REMOTE_PORT,
                constants.ENVIRONMENTS_NODES_REMOTE_PUBLIC_ADDRESS,
                constants.ENVIRONMENTS_NODES_REMOTE_PUBLIC_PORT,
                constants.ENVIRONMENTS_NODES_REMOTE_USERNAME,
                constants.ENVIRONMENTS_NODES_REMOTE_PASSWORD,
                constants.ENVIRONMENTS_NODES_REMOTE_PRIVATEKEYFILE,
            ]
            parameters: Dict[str, Any] = dict()
            for key in config:
                if key in fields:
                    parameters[key] = cast(str, config[key])
            node.set_connection_info(**parameters)
    return node


def from_spec(
    spec: Dict[str, object], node_type: str = constants.ENVIRONMENTS_NODES_REMOTE
) -> Node:
    is_default = cast(bool, spec.get(constants.IS_DEFAULT, False))
    node = Node.create("spec", spec=spec, node_type=node_type, is_default=is_default)
    return node
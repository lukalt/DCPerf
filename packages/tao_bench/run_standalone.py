#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import os
import pathlib
import re
import subprocess
import threading

import args_utils

from run_autoscale import gen_client_instructions

BENCHPRESS_ROOT = pathlib.Path(os.path.abspath(__file__)).parents[2]
TAO_BENCH_DIR = os.path.join(BENCHPRESS_ROOT, "packages", "tao_bench")
TAO_BENCH_BM_DIR = os.path.join(BENCHPRESS_ROOT, "benchmarks", "tao_bench")


# User setting either server_port_number or port_number_start will result in the same port number between client and server
class SyncPortAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        if self.dest == "server_port_number":
            namespace.port_number_start = values
        elif self.dest == "port_number_start":
            namespace.server_port_number = values


# User setting either server_port_number or port_number_start will result in the same port number between client and server
class SyncMemsizeAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        if self.dest == "server_memsize":
            namespace.memsize = values
        elif self.dest == "memsize":
            namespace.server_memsize = values


SERVER_CMD_OPTIONS = []  # To be initialized in init_parser()


def init_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        conflict_handler="resolve",
    )
    global SERVER_CMD_OPTIONS
    SERVER_CMD_OPTIONS = args_utils.add_common_server_args(parser)
    args_utils.add_common_client_args(parser)

    # Override the default values for server-side arguments
    parser.add_argument("--server-hostname", type=str, help="server hostname")
    parser.add_argument(
        "--server-memsize",
        type=float,
        help="server memory size, e.g. 64, 96",
        action=SyncMemsizeAction,
    )

    for action in parser._actions:
        if action.dest == "server_port_number":
            action.__class__ = SyncPortAction
        elif action.dest == "memsize":
            action.__class__ = SyncMemsizeAction

    parser.add_argument(
        "--num-servers",
        type=int,
        default=args_utils.get_default_num_servers(),
        help="number of TaoBench server instances",
    )
    parser.add_argument(
        "--num-clients",
        type=int,
        default=2,
        help="number of clients to use. This parameter is used for generating client side commands and instructions.",
    )
    parser.add_argument(
        "--port-number-start",
        type=int,
        default=11211,
        help="starting port number of the servers",
        action=SyncPortAction,
    )
    parser.add_argument(
        "--bind-cpu",
        type=int,
        default=1,
        help="explicitly bind TaoBench server instances to dedicated CPU sockets on machines with "
        + "multiple NUMA nodes to minimize cross-socket traffic.",
    )
    parser.add_argument(
        "--bind-mem",
        type=int,
        default=1,
        help="explicitly bind TaoBench server instances to the memory node local to the CPU cores "
        + "on machines with multiple NUMA nodes in order to minimize cross-socket traffic. "
        + "Please set this to 0 if you would like to test hetereogeneous memory systems such as CXL.",
    )
    parser.add_argument(
        "--clients-per-thread",
        type=int,
        default=args_utils.sanitize_clients_per_thread(380),
        help="Number of clients per thread",
    )
    parser.add_argument(
        "--client-cores",
        type=int,
        default=0,
        help="number of logical CPU cores on the client machine. "
        + "If not specified, we will assume the client machine has the same number of cores as this server machine. "
        + "This parameter is used for suggesting clients_per_thread parameter on the client side in accompany with "
        + "'--conns-per-server-core'.",
    )
    parser.add_argument(
        "--conns-per-server-core",
        type=int,
        default=0,
        help="number of client connections per server core to impose. When set to a positive number"
        + "this is used for calculating clients_per_thread parameter to be used on the client side. "
        + "If `--clients-per-thread` is set to a positive number, this parameter will be ignored. ",
    )
    parser.add_argument(
        "--client-wait-after-warmup",
        type=int,
        default=-1,
        help="time in seconds for the client to wait after warmup before starting the test. "
        + " If set to 0 or positive, this will be used in the client instructions.",
    )
    parser.add_argument(
        "--num-client-threads",
        type=int,
        default=0,
        help="number of client threads to use. If not specified, will use default calculation (cores - 6).",
    )
    parser.add_argument(
        "--num-fast-threads",
        type=int,
        default=0,
        help="number of fast threads for the server. If not specified, will use default calculation (cores * fast_threads_ratio).",
    )
    parser.add_argument(
        "--num-slow-threads",
        type=int,
        default=0,
        help="number of slow threads for the server. If not specified, will use default calculation (fast_threads * slow_to_fast_ratio).",
    )
    return parser


def exec_cmd(cmd, output_file=subprocess.PIPE):
    p = subprocess.Popen(
        cmd, shell=True, stdout=output_file, stderr=output_file, text=True, bufsize=1
    )
    stdout, stderr = p.communicate()
    exitcode = p.returncode
    if exitcode != 0:
        print("Command exited with non-zero: " + cmd)
        print("ERROR: " + str(stderr))
        print("STDOUT: " + str(stdout))
    return stdout, stderr, exitcode


def launch_server(port_number_start=11211, bind_cpu=1, bind_mem=1):
    script_args = {
        optstr: getattr(args, argkey) for optstr, argkey in SERVER_CMD_OPTIONS
    }
    script_args["--interface-name"] = "lo"
    script_args["--client-wait-after-warmup"] = 0
    script_args["--timeout-buffer"] = 0
    if port_number_start > 0:
        script_args["--port-number-start"] = port_number_start
    script_args["--bind-cpu"] = bind_cpu
    script_args["--bind-mem"] = bind_mem

    # Add custom thread parameters if specified
    if hasattr(args, "num_fast_threads") and args.num_fast_threads > 0:
        script_args["--num-fast-threads"] = args.num_fast_threads
    if hasattr(args, "num_slow_threads") and args.num_slow_threads > 0:
        script_args["--num-slow-threads"] = args.num_slow_threads

    cmd = [f"{TAO_BENCH_DIR}/run_autoscale.py --real"]

    for argname, argval in script_args.items():
        if isinstance(argval, bool):
            if argval:
                cmd.append(argname)
        elif argval is not None:
            cmd.extend([argname, str(argval)])

    cmd_str = " ".join(cmd)
    stdout, stderr, exitcode = exec_cmd(cmd_str)
    print(stdout)


def launch_client(cmd, n=1):
    # Use benchpress dry-run to get the real client command
    stdout, stderr, exitcode = exec_cmd(cmd + " --dry-run")
    match = re.search(r"Execution command: (.*)$", stdout)
    if not match:
        print("ERROR: Unable to find the real client command in the output")
        print("STDOUT: " + str(stdout))
        print("STDERR: " + str(stderr))
        exit(1)
    real_cmd = match.group(1)
    with open(f"client_{n}.log", "w") as f:
        _, _, exitcode = exec_cmd(real_cmd, output_file=f)
    return exitcode


if __name__ == "__main__":
    parser = init_parser()
    args = parser.parse_args()
    if args.num_servers == 0:
        args.num_servers = args_utils.get_default_num_servers()
    if args.memsize == 0:
        # Set memory size to 75% of system memory in the standalone mode to avoid OOM,
        # because the clients will also use memory on the same system
        args.memsize = args_utils.get_system_memsize_gb() * 0.75
    if args.warmup_time == 0:
        args.warmup_time = args_utils.get_warmup_time(args)
    args.server_memsize = args.memsize
    args.server_hostname = "localhost"

    t_server = threading.Thread(
        target=launch_server,
        args=(
            args.port_number_start,
            args.bind_cpu,
            args.bind_mem,
        ),
    )
    t_server.start()

    cmds = gen_client_instructions(args, to_file=False)
    clients = []
    for cmd in cmds.split("\n"):
        if "benchpress" in cmd:
            clients.append(cmd.strip())

    t_clients = []
    for n, client in enumerate(clients):
        cmd = str(BENCHPRESS_ROOT) + client[1:]
        tc = threading.Thread(
            target=launch_client,
            args=(
                cmd,
                n,
            ),
        )
        tc.start()
        t_clients.append(tc)

    for thread in t_clients:
        thread.join()

    t_server.join()

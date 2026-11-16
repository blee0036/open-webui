#!/usr/local/bin/python3
"""Keep Chromium's thread pools proportional to the one-CPU workspace budget.

Chromium sizes pools from host CPU affinity rather than Docker's CPU quota. On
large hosts its default pools exhaust the workspace's 128-PID limit at startup.
Affinity is inherited only by this browser process tree, not the terminal or
Python processes. The container's resource and network boundaries stay intact.
"""

import os
import sys

cpus = sorted(os.sched_getaffinity(0))
os.sched_setaffinity(0, {cpus[sum(os.environ.get('HOSTNAME', '').encode()) % len(cpus)]})
binary = '/usr/bin/chromium-upstream'
os.execv(
    binary,
    [
        binary,
        '--renderer-process-limit=1',
        '--num-raster-threads=1',
        '--disable-gpu',
        '--disable-software-rasterizer',
        '--disable-background-networking',
        *sys.argv[1:],
    ],
)

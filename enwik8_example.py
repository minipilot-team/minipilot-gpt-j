import os

import jax
import numpy as np
import optax


# Run with a bunch of CPU devices.
from loader import TextLoader


def setUpModule():
    global prev_xla_flags
    prev_xla_flags = os.getenv("XLA_FLAGS")
    flags_str = prev_xla_flags or ""
    if "xla_force_host_platform_device_count" not in flags_str:
        os.environ["XLA_FLAGS"] = (flags_str + " --xla_force_host_platform_device_count=8")


setUpModule()


from transformer_shard import CausalTransformer

loader = TextLoader("data/enwik8", 8, 65)

devices = np.array(jax.devices()).reshape((2, 4))

with jax.experimental.maps.mesh(devices, ('batch', 'shard')):
    opt = optax.chain(
        optax.clip_by_global_norm(1),
        optax.scale_by_adam(eps=1e-4),
        optax.scale(-1e-4),
    )

    c = CausalTransformer(128, 4, 4, 256, opt)

    while True:
        sample = loader.get_samples()
        loss = c.train(sample)

        print(loss.mean())
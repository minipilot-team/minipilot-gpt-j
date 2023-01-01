import argparse
import json
import threading
import time
from eventlet.queue import LightQueue, Empty

import jax
import numpy as np
import optax
import asyncio

from mesh_transformer import util
from mesh_transformer.checkpoint import read_ckpt
from mesh_transformer.sampling import nucleaus_sample
from mesh_transformer.transformer_shard import CausalTransformer
import transformers
from smart_open import open

from mesh_transformer.util import clip_by_global_norm

# from flask import Flask, request, make_response, jsonify
# app = Flask(__name__)

requests_queue = LightQueue()

"""
curl --header "Content-Type: application/json" \
  --request POST \
  --data '{"context":"eleutherai", "top_p": 0.9, "temp": 0.75}' \
  http://localhost:5000/complete
"""

import socketio
import eventlet
import json
from contracts.hint import hint_response, hint_request, hint



with open("config.json") as json_data_file:
    config = json.load(json_data_file)


sio = socketio.Server(async_mode='eventlet')
app = socketio.WSGIApp(sio)

@sio.event
def connect(sid, environ):
    print('connect ', sid)

@sio.event
def get_completions(sid, packed_data):
    data = hint_request(**(json.loads(packed_data)))
    print("Received:")
    print(data.text)
    if requests_queue.qsize() > 100:
        return {"error": "queue full, try again later"}
    
    response_queue = LightQueue()

    requests_queue.put(({
                            "context": data.text,
                            "top_p": float(0.9),
                            "temp": float(1.0)
                        }, response_queue))
    requests_queue.put(({
                            "context": data.text,
                            "top_p": float(0.9),
                            "temp": float(1.0)
                        }, response_queue))

    response_text = response_queue.get()
    response_text2 = response_queue.get()
    extracted_hints = [hint(response_text, 0), hint(response_text2, 0)] 

    response = hint_response(data.id, extracted_hints)

    print("Response:")
    print(response_text)
    print("-------")
    print(response_text2)
    sio.emit("receive_completions", json.dumps(response.__dict__))

@sio.event
def disconnect(sid):
    print('disconnect ', sid)


# @app.route('/complete', methods=['POST', 'OPTIONS'])
# def complete():
#     if request.method == "OPTIONS":  # CORS preflight
#         return _build_cors_prelight_response()
#     elif request.method == "POST":  # The actual request following the preflight
#         content = request.json

#         if requests_queue.qsize() > 100:
#             return {"error": "queue full, try again later"}

#         response_queue = Queue()

#         requests_queue.put(({
#                                 "context": content["context"],
#                                 "top_p": float(content["top_p"]),
#                                 "temp": float(content["temp"])
#                             }, response_queue))

#         return _corsify_actual_response(jsonify({"completion": response_queue.get()}))
#     else:
#         raise RuntimeError("Weird - don't know how to handle method {}".format(request.method))


def parse_args():
    # Parse command line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, default="./configs/minipilot.json", help="Config file location")

    args = parser.parse_args()
    return args

def server():
    eventlet.wsgi.server(eventlet.listen((config["address"], config["port"])), app)

def predictor(params):
    gradient_accumulation_steps = params.get("gradient_accumulation_steps", 1)
    per_replica_batch = params["per_replica_batch"]
    cores_per_replica = params["cores_per_replica"]

    assert cores_per_replica <= 8

    bucket = params["bucket"]
    model_dir = params["model_dir"]
    # layers = params["layers"]
    # d_model = params["d_model"]
    # n_heads = params["n_heads"]
    # n_vocab = params["n_vocab"]
    seq = params["seq"]
    # norm = params["norm"]

    params["sampler"] = nucleaus_sample
    opt = optax.chain(
        optax.scale(1 / gradient_accumulation_steps),
        clip_by_global_norm(1),
        optax.scale_by_adam(),
        optax.additive_weight_decay(0),
        optax.scale(-1),
        optax.scale_by_schedule(util.gpt3_schedule(0, 1, 0, 0))
    )

    params["optimizer"] = opt

    start = time.time()
    print(f"jax devices: {jax.device_count()}")
    print(f"jax runtime initialized in {time.time() - start:.06}s")

    mesh_shape = (jax.device_count() // cores_per_replica, cores_per_replica)
    devices = np.array(jax.devices()).reshape(mesh_shape)

    #with open(f"gs://{bucket}/{model_dir}/meta.json", "r") as f:
    #    meta = json.load(f)

    ckpt_step = 39637
    print(f"using checkpoint {ckpt_step}")

    total_batch = per_replica_batch * jax.device_count() // cores_per_replica * 8
    with jax.experimental.maps.mesh(devices, ('dp', 'mp')):
        network = CausalTransformer(params)

        start = time.time()
        network.state = read_ckpt(network.state, f"gs://{bucket}/{model_dir}/step_{ckpt_step}/", devices.shape[1])
        print(f"network loaded in {time.time() - start:.06}s")

        local_shards = max(jax.local_device_count() // mesh_shape[1], 1)
        del network.state["opt_state"]
        network.state = network.move_xmap(network.state, np.zeros(local_shards))

        tokenizer = transformers.GPT2TokenizerFast.from_pretrained('gpt2')

        while True:
            all_ctx = []
            all_top_p = []
            all_temp = []
            all_q = []
            while len(all_ctx) < total_batch:
                try:
                    o, q = requests_queue.get(block=False)
                    all_ctx.append(o["context"])
                    all_top_p.append(o["top_p"])
                    all_temp.append(o["temp"])
                    all_q.append(q)
                except Empty:
                    if len(all_ctx):
                        break
                    else:
                        eventlet.sleep(0.01)

            start = time.time()
            while len(all_ctx) < total_batch:
                all_ctx.append("whatever")
                all_top_p.append(1)
                all_temp.append(1)

            all_tokenized = []
            all_length = []
            for ctx in all_ctx:
                padded_tokens = np.zeros(seq).astype(np.uint32)
                length = 0

                try:
                    tokens = tokenizer.encode(ctx)
                    provided_ctx = len(tokens)
                    pad_amount = seq - provided_ctx

                    pad_amount = max(pad_amount, 0)

                    padded_tokens = np.pad(tokens, ((pad_amount, 0),)).astype(np.uint32)[-seq:]
                    length = len(tokens)
                except:
                    print("oops exception")

                all_tokenized.append(padded_tokens)
                all_length.append(length)
            print(f"only tokenizer encode done in {time.time() - start:06}s")
            
            start2 = time.time()
            output = network.generate(np.array(all_tokenized),
                                      np.array(all_length),
                                      32,
                                      {
                                          "top_p": np.array(all_top_p),
                                          "temp": np.array(all_temp)
                                      })
            print(f"only inference done in {time.time() - start2:06}s")
            
            start3 = time.time()
            for o, q in zip(output[1][0][:, :, 0], all_q):
                q.put(tokenizer.decode(o))

            print(f"only tokenizer decode done in {time.time() - start3:06}s")

            print(f"all completion done in {time.time() - start:06}s")

if __name__ == "__main__":

    pool = eventlet.GreenPool()

    args = parse_args()
    params = json.load(open(args.config))
    pool.spawn(predictor, params)

    eventlet.wsgi.server(eventlet.listen((config["address"], config["port"])), app)


    # threading.Thread(target=app.run, kwargs={"port": 5000, "host": "0.0.0.0"}).start()
    # threading.Thread(target=eventlet.wsgi.server, args=(eventlet.listen((config["address"], config["port"])), app)).start()

    # eventlet.wsgi.server(eventlet.listen((config["address"], config["port"])), app)
    # eventlet.spawn(eventlet.wsgi.server, eventlet.listen((config["address"], config["port"])), app)

    # # eventlet.wsgi.server(eventlet.listen((config["address"], config["port"])), app)
    # while True:
    #     print("loop")
    #     eventlet.sleep(5) 



    

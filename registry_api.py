import time
from pymongo import MongoClient

client = MongoClient("mongodb://mongo1,mongo2,mongo3/?replicaSet=rs0")
models = client.evalhub.models
while True:
    t0 = time.perf_counter()
    models.find_one({"_id": "summarizer-v2"})
    print(f"lookup {(time.perf_counter() - t0) * 1000:6.2f} ms")
    time.sleep(0.1)

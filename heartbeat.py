import time, datetime
from pymongo import MongoClient

client = MongoClient("mongodb://mongo1,mongo2,mongo3/?replicaSet=rs0",
                     serverSelectionTimeoutMS=30000)
hb = client.evalhub.heartbeat

while True:
    t0 = time.perf_counter()
    hb.insert_one({"t": datetime.datetime.utcnow()})
    ms = (time.perf_counter() - t0) * 1000
    flag = "  <-- SLOW" if ms > 500 else ""
    print(f"{datetime.datetime.now():%H:%M:%S.%f}  {ms:8.1f} ms  primary={client.primary}{flag}")
    time.sleep(0.2)

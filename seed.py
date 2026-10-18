import random, datetime
from pymongo import MongoClient, InsertOne

client = MongoClient("mongodb://mongo1,mongo2,mongo3/?replicaSet=rs0")
db = client.evalhub

MODELS = [
    {"_id": "summarizer-v1", "type": "llm", "status": "retired",
     "params": {"context_window": 4096, "quantization": "fp16"}},
    {"_id": "summarizer-v2", "type": "llm", "status": "production",
     "params": {"context_window": 8192, "quantization": "int8"}},
    {"_id": "summarizer-v3", "type": "llm", "status": "staging",
     "params": {"context_window": 8192, "quantization": "int8"}},
    {"_id": "embedder-v1", "type": "embedding", "status": "production",
     "params": {"dimensions": 768, "pooling": "mean"}},
    {"_id": "intent-clf-v4", "type": "classifier", "status": "staging",
     "params": {"labels": 42, "threshold": 0.65}},
]
SUITES = ["summarization-v2", "qa-v1", "safety-v3", "retrieval-v1"]

db.models.delete_many({})
db.models.insert_many([{**m, "created_at": datetime.datetime.utcnow()} for m in MODELS])

start = datetime.datetime(2026, 1, 1)
results_written = 0
for r in range(300):                                 # 300 runs
    model = random.choice(MODELS)["_id"]
    suite = random.choice(SUITES)
    started = start + datetime.timedelta(hours=r * 6)
    run_id = db.eval_runs.insert_one({
        "model_id": model, "suite": suite, "status": "running",
        "started_at": started, "config": {"temperature": 0.2, "prompts": 1000},
    }).inserted_id

    batch, latencies, scores = [], [], []
    for p in range(1000):                            # 1,000 prompts per run → 300k results
        lat, score = random.randint(80, 900), random.random()
        latencies.append(lat); scores.append(score)
        batch.append(InsertOne({
            "run_id": run_id, "model_id": model, "prompt_id": f"{suite}-{p:05d}",
            "latency_ms": lat, "output_tokens": random.randint(20, 400),
            "scores": {"judge": round(score * 5), "rouge_l": round(score * 0.6, 3)},
            "ts": started + datetime.timedelta(seconds=p * 2),
            "pad": "x" * random.randint(200, 1200),  # realistic doc size
        }))
    db.eval_results.bulk_write(batch, ordered=False)
    results_written += len(batch)

    latencies.sort()
    db.eval_runs.update_one({"_id": run_id}, {"$set": {
        "status": "completed",
        "finished_at": started + datetime.timedelta(minutes=40),
        "aggregate": {"score": round(sum(scores) / len(scores), 3),
                      "latency_p95_ms": latencies[int(len(latencies) * 0.95)]},
    }})
    if r % 25 == 0:
        print(f"run {r}: {results_written:,} results")
print("done")

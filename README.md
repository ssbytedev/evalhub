# evalhub

A small local lab for watching how a **MongoDB replica set** behaves under real
traffic: elections, failover, stalled writes, read latency and replication lag.

The data is modeled on a made-up ML evaluation platform ("evalhub"). It keeps a
registry of models, runs of eval suites against those models, and the
per-prompt results of each run. That gives the database a realistic shape and
volume (about 300k documents) without needing a real application.

## Why it exists

Replica sets are easy to describe and hard to understand until you watch one
fail. Docs say "an election takes a few seconds" and "writes block without a
majority". This lab lets you see that on your own machine:

- Kill the primary and see exactly how long writes stall.
- See which client settings make reads keep working during failover and which
  don't.
- Put a secondary behind and watch it catch up from the oplog.

Everything runs in Docker and you can reset it in seconds, so breaking things
costs nothing.

## Files

| File | Role |
|---|---|
| `docker-compose.yml` | Starts three `mongo:8.0` containers (`mongo1`, `mongo2`, `mongo3`) as members of replica set `rs0`. The oplog is capped at 990 MB and the WiredTiger cache at 0.25 GB per node. These limits are deliberately small so that cache pressure and oplog behavior show up with a modest dataset. No ports are published: clients reach the nodes by container name on the Compose network. |
| `seed.py` | Fills the `evalhub` database. It replaces the `models` collection with 5 models (LLMs, an embedder and a classifier at different lifecycle stages). It then creates 300 `eval_runs` (one every 6 hours starting 2026-01-01) and writes 1,000 `eval_results` per run, about 300k in total. Each result gets a random 200–1200 byte `pad` field so documents are a realistic size. After each run's results are in, the run is marked `completed` with its average score and p95 latency. |
| `heartbeat.py` | **Write probe.** Inserts a document into `evalhub.heartbeat` every 200 ms. For each insert it prints the latency and which node is currently primary, and marks any write over 500 ms with `<-- SLOW`. Its 30 s server-selection timeout lets it wait through an election instead of crashing. |
| `registry_api.py` | **Read probe.** Stands in for a model-registry service. Every 100 ms it looks up the `summarizer-v2` model by `_id` and prints how long the lookup took. |

### How the pieces fit

```
            Compose network (evalhub_default)
  ┌─────────────────────────────────────────────┐
  │   mongo1 ◄──► mongo2 ◄──► mongo3   (rs0)    │
  │      ▲  one primary, two secondaries         │
  │      │                                       │
  │   python container: seed / heartbeat /       │
  │                     registry_api             │
  └─────────────────────────────────────────────┘
```

All scripts connect with `mongodb://mongo1,mongo2,mongo3/?replicaSet=rs0`. The
driver discovers the topology, sends writes (and, by default, reads) to the
current primary, and switches automatically when a new primary is elected.
The probes measure what that switch looks like from the client's side.

## Setup

Requirements: Docker with Compose v2. Python is not needed on the host because
the scripts run in a container.

**1. Start the nodes**

```sh
docker compose up -d
```

**2. Initiate the replica set** (once, after the first `up`)

```sh
docker exec mongo1 mongosh --quiet --eval '
rs.initiate({_id: "rs0", members: [
  {_id: 0, host: "mongo1:27017"},
  {_id: 1, host: "mongo2:27017"},
  {_id: 2, host: "mongo3:27017"}
]})'
```

Wait a few seconds, then confirm there is one `PRIMARY` and two `SECONDARY`:

```sh
docker exec mongo1 mongosh --quiet --eval 'rs.status().members.map(m => m.name + " " + m.stateStr)'
```

**3. Run the scripts**

The hostnames `mongo1..3` only resolve inside the Compose network, so run the
scripts from a Python container attached to it. Define a helper in your shell:

```sh
evpy() {
  docker run --rm -it --network evalhub_default \
    -v "$PWD":/app -w /app python:3.12-slim \
    sh -c "pip install -q pymongo && python $1"
}
```

> The network is named `<directory>_default`. If you cloned into a directory
> that isn't called `evalhub`, check the name with `docker network ls`.

```sh
evpy seed.py          # load data (takes a few minutes; prints progress every 25 runs)
evpy heartbeat.py     # terminal 2: write probe
evpy registry_api.py  # terminal 3: read probe
```

`seed.py` only resets `models`. If you run it again, it adds another 300 runs
and 300k results instead of replacing them. To start clean, see
[Reset](#reset).

## Experiments

Keep `heartbeat.py` and `registry_api.py` running in two terminals and use a
third terminal to break things. To find the current primary at any time:

```sh
docker exec mongo1 mongosh --quiet --eval 'rs.hello().primary'
```

### 1. Kill the primary

```sh
docker stop mongo1        # use whichever node is primary
```

What to watch:

- **heartbeat:** a gap and a few `SLOW` lines, then `primary=` changes to
  another node. The length of the gap is your real failover time.
- **registry_api:** lookups stall too, because reads go to the primary by
  default.

Bring the node back with `docker start mongo1`. It rejoins as a secondary and
catches up.

### 2. Graceful stepdown vs. a crash

```sh
docker exec <primary> mongosh --quiet --eval 'rs.stepDown(30)'
```

Compare the stall with experiment 1. A stepdown hands over to a caught-up
secondary right away. After a crash, the other nodes first have to notice the
primary is gone (`electionTimeoutMillis`, 10 s by default) before they can
elect a new one.

### 3. Keep reads working during failover

Edit the connection string in `registry_api.py`:

```python
"mongodb://mongo1,mongo2,mongo3/?replicaSet=rs0&readPreference=secondaryPreferred"
```

Repeat experiment 1. Reads should continue through the election while writes
in `heartbeat.py` still stall. The tradeoff is that reads from a secondary can
be slightly stale. Try `primaryPreferred` and `nearest` as well.

### 4. Lose the majority

Stop **two** nodes:

```sh
docker stop mongo2 mongo3
```

The remaining node cannot win an election alone, so it steps down to
secondary. Writes hang until the 30 s server-selection timeout and then fail.
Reads still work if you set a secondary read preference. Start one node again
and see how quickly a primary comes back.

### 5. Network partition instead of a crash

```sh
docker network disconnect evalhub_default <primary>
# ...watch...
docker network connect evalhub_default <primary>
```

The isolated node keeps running but cannot see a majority, so it steps down.
When it reconnects, any writes it accepted that never replicated are rolled
back. Look for `rollback` in `docker logs <node>`.

### 6. Replication lag

Pause a secondary, generate writes, then resume it:

```sh
docker pause mongo3
evpy seed.py                     # heavy write load
docker exec mongo1 mongosh --quiet --eval 'rs.printSecondaryReplicationInfo()'
docker unpause mongo3
```

Run the replication-info command again a few times and watch the lag go down.
The oplog is capped at 990 MB. If a secondary falls further behind than the
oplog covers, it can't catch up and needs a full resync.

### 7. Queries and indexes on a small cache

`seed.py` creates no indexes apart from `_id`, and each node has only 0.25 GB of
cache, which is smaller than the `eval_results` collection. Open a shell:

```sh
docker exec -it mongo1 mongosh evalhub
```

```js
db.eval_results.find({model_id: "summarizer-v2", latency_ms: {$gt: 800}})
  .explain("executionStats").executionStats   // COLLSCAN, ~300k docs examined

db.eval_results.createIndex({model_id: 1, latency_ms: 1})
// re-run the explain: IXSCAN, far fewer docs examined

db.eval_runs.aggregate([
  {$match: {status: "completed"}},
  {$group: {_id: "$model_id", avg: {$avg: "$aggregate.score"},
            p95: {$avg: "$aggregate.latency_p95_ms"}}}
])
```

Try building an index while the probes run and check whether `heartbeat.py`
latency goes up.

### Other ideas

- Give one node `priority: 2` with `rs.reconfig()` and watch it take back the
  primary role whenever it is healthy.
- Add `&w=1` to `heartbeat.py`'s URI and compare write latency with the default
  `w: "majority"`. Then think about what `w=1` loses in experiment 5.
- Lower `electionTimeoutMillis` in the replica set config and measure the
  failover gap again.

## Reset

```sh
docker compose down -v    # removes the containers and their data
docker compose up -d      # then repeat step 2 (rs.initiate)
```

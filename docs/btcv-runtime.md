# BTCV runtime validation

The root Dockerfile targets Linux amd64, CPython 3.12 and LevelDB. It does
not include RocksDB or the native hash extensions used by other coins. Do
not change a running node's database engine to make it match the image.
Build from a source revision that includes every patch your node runs.

## Local qualification

Build once and test the resulting image ID rather than a mutable tag.
From the repository root in a POSIX shell:

```sh
docker build -t electrumx-btcv:candidate .
id=$(docker image inspect --format '{{.Id}}' electrumx-btcv:candidate)
for test in test_runtime_wallet.py test_runtime_transports.py; do
  docker run --rm --pull=never --network none \
    --mount "type=bind,source=$PWD/tests/integration,target=/smoke,readonly" \
    --entrypoint python3 "$id" -B "/smoke/$test" || exit 1
done
```

The wallet test uses a loopback Core RPC fixture. ElectrumX itself is the
installed server: it indexes serialized regtest blocks into a temporary
LevelDB, serves history and balance, then shuts down and reopens the index.
This is not a consensus test, a full-history sync, or a rehearsal on an
existing deployment's database. Do not supply real wallet data or credentials.

## Staging and rollback

1. Record the old image digest, database engine, tip height and hash. Take a
   consistent snapshot with the writer stopped or using a supported snapshot
   mechanism; verify it can be restored to a different volume.
2. Run the candidate against **a restored copy**, a staging daemon and no
   public traffic. Never let two ElectrumX writers open the same directory.
3. Compare the hash at a common height with the daemon. Check that the
   candidate follows new blocks, serves known staging history and balances
   over the configured transports, and flushes and restarts cleanly.
4. Rehearse rollback using the previous digest and the untouched snapshot.
   Verify the tip and wallet reads again. A healthy TCP probe alone is not
   a successful rollback.
5. Record observations and outstanding image scan findings before proposing
   a production canary. Stop if tip/hash or wallet responses diverge, the
   process repeatedly restarts, or storage recovery fails.

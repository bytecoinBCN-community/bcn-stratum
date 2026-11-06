# bcn-stratum

A solo stratum bridge between CryptoNight ASICs (tested with the Antminer X3) and `bytecoind` v3.5 (block v4).

This is an independent community project, not affiliated with the Bytecoin founders or bcndev.

Each miner connection gets its own block template, so several ASICs never search the same nonce space. Block parsing, the PoW hashing blob and CryptoNight all come from Bytecoin's own code through `libbcnhelper.so`.

## The block limiter

**The bridge can deliberately discard blocks found by your own miner.** It is published so anyone can see how it works.

One X3 can have more hashrate than the rest of the network. If all its blocks reached the chain, difficulty would follow it up, and when the miner stopped, the remaining miners would struggle for a long time because difficulty reacts over a 720-block window.

The limiter submits only a share of found blocks, so the miner counts on chain as `ratio x real hashrate`. It only ever affects your own blocks. It does not touch or reorganize anyone else's.

Settings are in a limit file, re-read every minute. See [limit.example.conf](limit.example.conf).

- **Fixed mode:** `submit_ratio` sets the share of found blocks to submit.
- **Auto mode:** `target_share` keeps your on-chain share near a target. The ratio changes at most x1.25 per step, within `min_ratio` and `max_ratio`. The first change can happen 24 hours after the bridge starts.

The current ratio is not saved across restarts. After a restart the bridge starts from `submit_ratio`, so set it to the value you want to resume from.

With no limit file, every block is submitted.

## Behaviour without peers

The bridge pauses jobs while `bytecoind` is behind or has no outgoing peers. This is intentional: mining without peers builds a private chain, whose blocks are lost on reconnection and could disturb the network. An ASIC may keep hashing its old job during a pause, so expect some stale shares.

## Build the helper

`libbcnhelper.so` is built from `src/bcnhelper.cpp` against a compiled Bytecoin tree. First build [the Ubuntu 24.04 branch](https://github.com/bytecoinBCN-community/bytecoin/tree/ubuntu-24.04), then from the directory that contains `bytecoin/`:

```bash
g++ -O2 -std=c++14 -fPIC -shared -I bytecoin/src -I bytecoin/vendor \
  -o bcn-stratum/libbcnhelper.so bcn-stratum/src/bcnhelper.cpp \
  -Wl,--start-group bytecoin/libs/libbytecoin-core.a bytecoin/libs/libbytecoin-crypto.a -Wl,--end-group \
  -Wl,--no-undefined -lpthread
```

The bridge needs only Python 3 and this library.

## Run

```bash
export BCN_WALLET_ADDRESS=your_public_address
export BCN_LIMIT_FILE=/etc/bcn-stratum-limit.conf
python3 bcn_stratum.py --listen 0.0.0.0:3333 --share-diff 60000
```

A systemd unit is in [bcn-stratum.service](bcn-stratum.service), and an environment file template in [bcn-stratum.env.example](bcn-stratum.env.example).

Point the miner at `stratum+tcp://<bridge-ip>:3333`. The worker name is only used in logs. The password is ignored, except for an optional `d=NNNN` that sets the share difficulty.

**Keep port 3333 and the node RPC (8081) inside your LAN.** The bridge has no authentication. It needs only your public address, so never give it a seed or private key.

## Tests

| File | Needs | What it checks |
| --- | --- | --- |
| `limittest.py` | Python | Limiter logic |
| `selftest.py` | helper, live node | Helper against real chain data |
| `src/e2etest.py` | helper, live node | Stratum session end to end against a fake daemon |
| `src/tmpltest.py` | helper, live node | Block template handling |

## Licence

LGPLv3, the same as Bytecoin, whose code the helper links. See [LICENSE](LICENSE).

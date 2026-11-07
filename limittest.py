#!/usr/bin/env python3
"""Unit test of the block limiter: exact fixed ratio, live reload, and auto mode convergence."""
import os
import sys
import tempfile
import time

sys.modules["bcnlib"] = type(sys)("bcnlib")  # the limiter does not need the native helper
import bcn_stratum as bs  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok &= bool(cond)
    print("%-48s %s" % (name, "ok" if cond else "FAIL"))


path = os.path.join(tempfile.mkdtemp(), "limit.conf")
with open(path, "w") as f:
    f.write("# start small\nsubmit_ratio = 0.02\n")
lim = bs.Limiter(path)
check("fixed ratio read from file", abs(lim.ratio - 0.02) < 1e-12 and not lim.auto)
sent = sum(lim.should_submit() for _ in range(1000))
check("exactly 20 of 1000 blocks submitted", sent == 20)

time.sleep(0.01)
with open(path, "w") as f:
    f.write("submit_ratio = 0.5\n")
os.utime(path, (time.time() + 5, time.time() + 5))
lim.reload()
check("live reload to 0.5", abs(lim.ratio - 0.5) < 1e-12)
sent = sum(lim.should_submit() for _ in range(100))
check("50 of 100 submitted after reload", sent == 50)

check("no file -> every block", bs.Limiter(os.path.join(os.path.dirname(path), "none.conf")).ratio == 1.0)

# auto mode: X3 220 KH/s, others 9 KH/s, target 50% -> ratio should approach 9/220 ~ 0.041
with open(path, "w") as f:
    f.write("submit_ratio = 0.02\ntarget_share = 0.5\nmin_ratio = 0.01\nmax_ratio = 0.2\nadjust_hours = 6\n")
os.utime(path, (time.time() + 10, time.time() + 10))
lim = bs.Limiter(path)
check("auto mode on, starts at 0.02", lim.auto and abs(lim.ratio - 0.02) < 1e-12)
clock = [time.time()]
bs.time.time = lambda: clock[0]
lim.started = lim.last_adjust = clock[0]
X3, OTHERS = 220000.0, 9000.0
onchain = []
net_diff = (OTHERS + X3 * lim.ratio) * bs.BLOCK_TARGET
for minute in range(14 * 24 * 60):
    clock[0] += 60
    # the X3 sends only its own hard shares: share work shows ~1/30 of it, block candidates show all of it
    lim.record(X3 * 60 / 30, X3 * 60, X3 * 60 / net_diff, 60)
    onchain.append(X3 * lim.ratio)
    onchain = onchain[-24 * 60:]
    net_diff = (OTHERS + sum(onchain) / len(onchain)) * bs.BLOCK_TARGET  # difficulty follows a 1-day window
    lim.adjust(net_diff)
want = OTHERS / X3
check("auto ratio converges to ~%.4f (got %.4f)" % (want, lim.ratio), abs(lim.ratio - want) / want < 0.1)
share = X3 * lim.ratio / (X3 * lim.ratio + OTHERS)
check("our on-chain share ~50%% (got %.0f%%)" % (share * 100), abs(share - 0.5) < 0.05)
raw = lim.estimate(net_diff)[0]
check("raw hashrate taken from candidates (got %.0f KH/s)" % (raw / 1000), abs(raw - X3) / X3 < 0.01)

# a lucky or an unlucky last hour must not change the raw estimate: 24 h of 190 KH/s, last hour 280 or nothing
X3 = 190000.0
net_diff = 190000.0 * bs.BLOCK_TARGET


def day_with_last_hour(rate):
    return [(clock[0] - 60 * i, X3 * 60 / 30, (rate if i < 60 else X3) * 60, (rate if i < 60 else X3) * 60 / net_diff,
             0.41, 60) for i in range(24 * 60)]


lim.history = day_with_last_hour(280000.0)
raw = lim.estimate(net_diff)[0]
check("lucky hour barely moves raw (got %.0f KH/s)" % (raw / 1000), abs(raw - X3) / X3 < 0.03)
lim.history = day_with_last_hour(0.0)
raw = lim.estimate(net_diff)[0]
check("empty hour barely moves raw (got %.0f KH/s)" % (raw / 1000), abs(raw - X3) / X3 < 0.05)

# the ratio stays at its ceiling through such an hour
with open(path, "w") as f:
    f.write("submit_ratio = 0.41\ntarget_share = 0.46\nmin_ratio = 0.01\nmax_ratio = 0.41\nadjust_hours = 6\n")
os.utime(path, (time.time() + 20, time.time() + 20))
lim = bs.Limiter(path)
lim.started = lim.last_adjust = clock[0] - 2 * bs.DIFFICULTY_SPAN
lim.history = day_with_last_hour(280000.0)
lim.adjust(net_diff)
check("lucky hour leaves ratio at 0.41 (got %.4f)" % lim.ratio, abs(lim.ratio - 0.41) < 1e-9)

# too few candidates in the whole window -> fall back to share work
lim.history = [(clock[0] - 60 * i, 6000.0 * 60, 0.0, 0.0, 0.02, 60) for i in range(60)]
check("share work used when no candidates", abs(lim.estimate(net_diff)[0] - 6000.0) < 1)
print("LIMITTEST", "PASSED" if ok else "FAILED")
sys.exit(0 if ok else 1)

"""Atomic standalone Redis admission and pessimistic cost reservation."""

import json

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

CLAIM = """
local existing=redis.call('HGET',KEYS[1],'fingerprint')
if existing then
 if existing ~= ARGV[1] then return {'conflict'} end
 local result=redis.call('HGET',KEYS[1],'result')
 return {result and 'cached' or 'pending',result or ''}
end
local now=tonumber(redis.call('TIME')[1])
redis.call('ZREMRANGEBYSCORE',KEYS[2],'-inf',now)
if redis.call('ZCARD',KEYS[2])>=tonumber(ARGV[2]) then return {'concurrency'} end
local ratekey=KEYS[2]..':minute:'..math.floor(now/60)
local count=redis.call('INCR',ratekey)
redis.call('EXPIRE',ratekey,120)
if count>tonumber(ARGV[3]) then return {'rate_limit'} end
redis.call('HSET',KEYS[1],'fingerprint',ARGV[1],'accounted_nusd',0)
redis.call('EXPIRE',KEYS[1],86400)
redis.call('ZADD',KEYS[2],now+120,KEYS[1])
return {'claimed'}
"""

RESERVE = """
local prior=redis.call('HGET',KEYS[1],'reservation:'..ARGV[4])
if prior then return prior end
local now=tonumber(redis.call('TIME')[1])
local day=KEYS[2]..':day:'..math.floor(now/86400)
local current=tonumber(redis.call('HGET',day,'accounted_nusd') or '0')
local request=tonumber(redis.call('HGET',KEYS[1],'accounted_nusd') or '0')
local amount=tonumber(ARGV[1])
if request+amount>tonumber(ARGV[2]) or current+amount>tonumber(ARGV[3]) then return '' end
redis.call('HINCRBY',day,'accounted_nusd',amount)
redis.call('HINCRBY',day,'pending_reserved_nusd',amount)
redis.call('EXPIRE',day,172800)
redis.call('HINCRBY',KEYS[1],'accounted_nusd',amount)
redis.call('HSET',KEYS[1],'reservation:'..ARGV[4],day)
return day
"""

SETTLE = """
if redis.call('HGET',KEYS[1],'settled:'..ARGV[7]) then
 return redis.call('HGET',KEYS[1],'accounted_nusd')
end
local refund=tonumber(ARGV[1])-tonumber(ARGV[2])
redis.call('HINCRBY',KEYS[1],'accounted_nusd',-refund)
redis.call('HINCRBY',KEYS[2],'accounted_nusd',-refund)
redis.call('HINCRBY',KEYS[2],'pending_reserved_nusd',-tonumber(ARGV[1]))
redis.call('HINCRBY',KEYS[2],'input_tokens',ARGV[3])
redis.call('HINCRBY',KEYS[2],'output_tokens',ARGV[4])
redis.call('HINCRBY',KEYS[2],'attempts',1)
redis.call('HINCRBY',KEYS[2],'unknown_usage_attempts',ARGV[6])
redis.call('XADD',KEYS[3],'MAXLEN','~',1000,'*','request_id',ARGV[8],'attempt',ARGV[5])
redis.call('HSET',KEYS[1],'settled:'..ARGV[7],1)
return redis.call('HGET',KEYS[1],'accounted_nusd')
"""

FINISH = """
redis.call('HSET',KEYS[1],'result',ARGV[1])
redis.call('ZREM',KEYS[2],KEYS[1])
return 1
"""


class Ledger:
    def __init__(
        self, redis: Redis, prefix="gateway:lab", concurrency=4, rpm=60, daily_nusd=10_000_000
    ):
        self.redis = redis
        # Do not transparently replay ambiguous Redis writes. Scripts additionally
        # make per-model reservation/settlement idempotent within a request.
        self.redis.set_retry(Retry(NoBackoff(), 0))
        self.prefix = prefix
        self.concurrency = concurrency
        self.rpm = rpm
        self.daily_nusd = daily_nusd

    def key(self, identity):
        return f"{self.prefix}:request:{identity}"

    async def claim(self, identity, fingerprint):
        return await self.redis.eval(
            CLAIM,
            2,
            self.key(identity),
            self.prefix + ":active",
            fingerprint,
            self.concurrency,
            self.rpm,
        )

    async def reserve(self, identity, amount, request_limit, attempt):
        return await self.redis.eval(
            RESERVE,
            2,
            self.key(identity),
            self.prefix,
            amount,
            request_limit,
            self.daily_nusd,
            attempt,
        )

    async def settle(self, identity, day, reserved, actual, usage, attempt):
        return int(
            await self.redis.eval(
                SETTLE,
                3,
                self.key(identity),
                day,
                self.prefix + ":audit",
                reserved,
                actual,
                usage[0],
                usage[1],
                json.dumps(attempt),
                int(attempt["accounting"] == "unknown_usage_reserved_maximum"),
                attempt["model"],
                identity,
            )
        )

    async def finish(self, identity, result):
        await self.redis.eval(
            FINISH, 2, self.key(identity), self.prefix + ":active", json.dumps(result)
        )

    async def usage(self):
        now = int((await self.redis.time())[0])
        value = await self.redis.hgetall(f"{self.prefix}:day:{now // 86400}")
        return {
            k: int(value.get(k, 0))
            for k in [
                "accounted_nusd",
                "pending_reserved_nusd",
                "input_tokens",
                "output_tokens",
                "attempts",
                "unknown_usage_attempts",
            ]
        }

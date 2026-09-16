"""A/AAAA lookups against every resolver in the pool, in parallel.

Each (resolver, type) pair yields one Answer. CNAME chains in a response are
followed to the address RRset; the TTL is the minimum over the chain.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

import dns.asyncquery
import dns.exception
import dns.message
import dns.name
import dns.rcode
import dns.rdataclass
import dns.rdatatype

log = logging.getLogger(__name__)

RTYPES = ("A", "AAAA")
MAX_CNAME_HOPS = 16

QueryFn = Callable[[dns.message.Message, str, float], Awaitable[dns.message.Message]]


@dataclass(frozen=True)
class Answer:
    resolver: str
    rtype: str
    # noerror | nxdomain | servfail | refused | timeout | error
    status: str
    addresses: tuple[str, ...] = ()
    # Minimum TTL over the CNAME chain and the address RRset (None without addresses).
    ttl: int | None = None
    # Negative-caching TTL from the SOA in the authority section (RFC 2308).
    negative_ttl: int | None = None

    @property
    def definitive(self) -> bool:
        """The resolver gave a real answer (addresses, NODATA or NXDOMAIN)."""
        return self.status in ("noerror", "nxdomain")


def parse_response(response: dns.message.Message, qname: str, rtype: str, resolver: str) -> Answer:
    rcode = response.rcode()
    if rcode == dns.rcode.NXDOMAIN:
        return Answer(resolver, rtype, "nxdomain", negative_ttl=_negative_ttl(response))
    if rcode != dns.rcode.NOERROR:
        status = {dns.rcode.SERVFAIL: "servfail", dns.rcode.REFUSED: "refused"}.get(rcode, "error")
        return Answer(resolver, rtype, status)

    wanted = dns.rdatatype.from_text(rtype)
    name = dns.name.from_text(qname)
    ttls: list[int] = []
    for _ in range(MAX_CNAME_HOPS + 1):
        addresses = response.get_rrset(response.answer, name, dns.rdataclass.IN, wanted)
        if addresses is not None:
            ttls.append(addresses.ttl)
            return Answer(
                resolver,
                rtype,
                "noerror",
                addresses=tuple(sorted({rdata.address for rdata in addresses})),
                ttl=min(ttls),
            )
        cname = response.get_rrset(response.answer, name, dns.rdataclass.IN, dns.rdatatype.CNAME)
        if cname is None:
            break
        ttls.append(cname.ttl)
        name = cname[0].target

    # NODATA (or a chain that ends without addresses).
    return Answer(resolver, rtype, "noerror", negative_ttl=_negative_ttl(response))


def _negative_ttl(response: dns.message.Message) -> int | None:
    for rrset in response.authority:
        if rrset.rdtype == dns.rdatatype.SOA:
            return min(rrset.ttl, rrset[0].minimum)
    return None


async def _udp_with_tcp_fallback(
    request: dns.message.Message, where: str, timeout: float
) -> dns.message.Message:
    response, _ = await dns.asyncquery.udp_with_fallback(request, where, timeout=timeout)
    return response


class ResolverPool:
    def __init__(
        self,
        resolvers: Iterable[str],
        timeout: float,
        query: QueryFn = _udp_with_tcp_fallback,
    ) -> None:
        self.resolvers = tuple(resolvers)
        self.timeout = timeout
        self._query = query

    async def resolve(self, qname: str) -> list[Answer]:
        """A and AAAA from every resolver, concurrently."""
        tasks = [
            self._ask(qname, rtype, resolver) for rtype in RTYPES for resolver in self.resolvers
        ]
        return list(await asyncio.gather(*tasks))

    async def _ask(self, qname: str, rtype: str, resolver: str) -> Answer:
        request = dns.message.make_query(qname, rtype, use_edns=0, payload=1232)
        try:
            response = await self._query(request, resolver, self.timeout)
        except dns.exception.Timeout:
            return Answer(resolver, rtype, "timeout")
        except (OSError, dns.exception.DNSException) as exc:
            log.warning("query %s %s @%s failed: %s", qname, rtype, resolver, exc)
            return Answer(resolver, rtype, "error")
        return parse_response(response, qname, rtype, resolver)

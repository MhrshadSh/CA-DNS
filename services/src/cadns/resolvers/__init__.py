"""Query the upstream public resolver pool (Multi-Resolver strategy)."""

from cadns.resolvers.pool import Answer, ResolverPool, parse_response

__all__ = ["Answer", "ResolverPool", "parse_response"]

"""Shared provenance-expression DAG.

Every derivation is an integer-valued expression built from

* **tokens** -- one per *base row*, identified by the table identity plus the
  full primary-key value tuple in declared primary-key order;
* **constants** -- ``1`` (a derivation that consumes no base row, e.g. a
  constant-only SELECT) and ``0`` (no derivation);
* **addition** -- alternatives (UNION ALL, duplicate derivations of the same
  output tuple);
* **multiplication** -- joint requirements (INNER JOIN / CROSS JOIN).

The graph is *shared*: nodes are interned by structural identity, so
``(a1+a2)*(b1+b2)`` has 7 nodes rather than a copy per derivation, and a
self-join multiplies the same token node twice (tokens are atoms, never
deduplicated as sets).  Building never expands products; evaluating the
expression after excluding tokens walks the DAG once per node with
memoisation, so deep and diamond-shaped graphs stay linear in node count.

Big integers: values are Python ``int``, so counts are arbitrary precision.
"""
from __future__ import annotations

from typing import Iterable, Optional


class NodeId:
    """Opaque identity of a graph node (an integer index)."""

    __slots__ = ("idx",)

    def __init__(self, idx: int):
        self.idx = int(idx)

    def __eq__(self, other) -> bool:
        return isinstance(other, NodeId) and self.idx == other.idx

    def __hash__(self) -> int:
        return hash(self.idx)

    def __int__(self) -> int:
        return self.idx

    def __repr__(self) -> str:
        return f"NodeId({self.idx})"


# A compact alias so annotations read naturally.
Annotation = NodeId


class TokenInfo:
    """Everything a graph leaf needs to point back at a real warehouse row."""

    __slots__ = ("token_id", "table", "table_identity", "pk_columns", "pk_values")

    def __init__(self, token_id, table, table_identity, pk_columns, pk_values):
        self.token_id = token_id
        self.table = table
        self.table_identity = table_identity  # canonical, unambiguous name
        self.pk_columns = tuple(pk_columns)
        self.pk_values = tuple(pk_values)  # TyValue objects

    def key(self) -> str:
        parts = [self.table_identity, *[v.key() for v in self.pk_values]]
        # Length-prefix the table name so separators can never be forged.
        return f"{len(self.table_identity)}:{self.table_identity}|" + "|".join(parts[1:])

    def encode(self) -> dict:
        return {
            "token": self.token_id,
            "table": self.table,
            "table_identity": self.table_identity,
            "pk_columns": list(self.pk_columns),
            "pk_values": [v.encode() for v in self.pk_values],
        }

    @classmethod
    def decode(cls, obj: dict) -> "TokenInfo":
        from .values import TyValue

        return cls(
            obj["token"],
            obj["table"],
            obj["table_identity"],
            obj["pk_columns"],
            [TyValue.decode(v) for v in obj["pk_values"]],
        )


# Node tags -------------------------------------------------------------
NODE_ONE = "one"
NODE_ZERO = "zero"
NODE_TOKEN = "token"
NODE_ADD = "add"
NODE_MUL = "mul"


class Universe:
    """The set of tokens seen during one build.

    Tokens are interned by (table identity, typed primary-key values), so the
    same base row referenced through two aliases is *one* token, while two
    different rows are separate tokens.  Self-joins multiply the same atom.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, int] = {}
        self.tokens: list[TokenInfo] = []
        self.tables: dict[str, dict] = {}

    def register_table(self, table_identity: str, table: str, pk_columns) -> None:
        info = self.tables.get(table_identity)
        if info is None:
            self.tables[table_identity] = {
                "table_identity": table_identity,
                "table": table,
                "pk_columns": list(pk_columns),
                "row_count": 0,
            }

    def note_row_loaded(self, table_identity: str) -> None:
        self.tables[table_identity]["row_count"] += 1

    def token(self, table_identity: str, table: str, pk_columns, pk_values) -> int:
        info = self.tables.get(table_identity)
        if info is None:
            info = {
                "table_identity": table_identity,
                "table": table,
                "pk_columns": list(pk_columns),
                "row_count": 0,
            }
            self.tables[table_identity] = info
        key_parts = [table_identity, *[v.key() for v in pk_values]]
        key = "\x1f".join(key_parts)
        existing = self._by_key.get(key)
        if existing is not None:
            return existing
        token_id = len(self.tokens)
        self._by_key[key] = token_id
        self.tokens.append(
            TokenInfo(token_id, table, table_identity, pk_columns, list(pk_values))
        )
        return token_id

    def find_token(self, table_identity: str, pk_values) -> Optional[int]:
        key = "\x1f".join([table_identity, *[v.key() for v in pk_values]])
        return self._by_key.get(key)

    def __len__(self) -> int:
        return len(self.tokens)


class ProvenanceGraph:
    """A shared DAG of provenance nodes over a fixed token universe."""

    def __init__(self, universe: Optional[Universe] = None) -> None:
        self.universe = universe if universe is not None else Universe()
        self.kinds: list[str] = []
        self.args: list[tuple[int, ...]] = []  # token id, or child node ids
        self._intern: dict[tuple, int] = {}

        # Canonical constants.
        self._one = self._new(NODE_ONE, ())
        self._zero = self._new(NODE_ZERO, ())

    # -- node construction --------------------------------------------------

    def _new(self, kind: str, args: tuple[int, ...]) -> int:
        key = (kind, args)
        found = self._intern.get(key)
        if found is not None:
            return found
        idx = len(self.kinds)
        self.kinds.append(kind)
        self.args.append(args)
        self._intern[key] = idx
        return idx

    @property
    def one(self) -> int:
        return self._one

    @property
    def zero(self) -> int:
        return self._zero

    def token(self, token_id: int) -> int:
        return self._new(NODE_TOKEN, (token_id,))

    def add(self, a: int, b: int) -> int:
        # No constant folding beyond structural interning: 1 + 1 is a count of
        # 2, and after exclusion the same node must evaluate to 2 as well.
        return self._new(NODE_ADD, (min(a, b), max(a, b)))

    def mul(self, a: int, b: int) -> int:
        return self._new(NODE_MUL, (min(a, b), max(a, b)))

    def sum(self, nodes: Iterable[int]) -> int:
        acc = None
        for n in nodes:
            acc = n if acc is None else self.add(acc, n)
        return self._zero if acc is None else acc

    def product(self, nodes: Iterable[int]) -> int:
        acc = None
        for n in nodes:
            acc = n if acc is None else self.mul(acc, n)
        return self._one if acc is None else acc

    # -- introspection ------------------------------------------------------

    @property
    def node_count(self) -> int:
        return len(self.kinds)

    @property
    def edge_count(self) -> int:
        total = 0
        for kind, args in zip(self.kinds, self.args):
            if kind in (NODE_ADD, NODE_MUL):
                total += len(args)
            elif kind == NODE_TOKEN:
                total += 1  # the edge to the base row
        return total

    def kind(self, node: int) -> str:
        return self.kinds[node]

    def children(self, node: int) -> tuple[int, ...]:
        return self.args[node]

    def describe(self, node: int, tokens: Optional[list[TokenInfo]] = None) -> str:
        """Human-readable expression string in the algebra."""
        memo: dict[int, str] = {}

        def render(n: int) -> str:
            if n in memo:
                return memo[n]
            kind = self.kinds[n]
            if kind == NODE_ONE:
                text = "1"
            elif kind == NODE_ZERO:
                text = "0"
            elif kind == NODE_TOKEN:
                tok = (tokens or self.universe.tokens)[self.args[n][0]]
                text = f"[{tok.table}:{','.join(v.display() for v in tok.pk_values)}]"
            elif kind == NODE_ADD:
                text = "(" + " + ".join(render(c) for c in self.args[n]) + ")"
            else:
                text = "(" + " * ".join(render(c) for c in self.args[n]) + ")"
            memo[n] = text
            return text

        return render(node)

    # -- evaluation ---------------------------------------------------------

    def evaluate(self, node: int, excluded: frozenset[int] = frozenset()) -> int:
        """Evaluate the expression with the given base tokens set to zero.

        Walks the shared DAG iteratively (no recursion limit) and memoises
        per node, so a diamond or deep chain is linear in node count.  A token
        excluded in *this* snapshot contributes 0; all other tokens 1.
        """
        memo: dict[int, int] = {}
        stack: list[tuple[int, bool]] = [(node, False)]
        while stack:
            n, ready = stack.pop()
            if n in memo:
                continue
            kind = self.kinds[n]
            if kind == NODE_ONE:
                memo[n] = 1
            elif kind == NODE_ZERO:
                memo[n] = 0
            elif kind == NODE_TOKEN:
                memo[n] = 0 if self.args[n][0] in excluded else 1
            else:
                if not ready:
                    stack.append((n, True))
                    for c in self.args[n]:
                        if c not in memo:
                            stack.append((c, False))
                    continue
                if kind == NODE_ADD:
                    memo[n] = sum(memo[c] for c in self.args[n])
                else:
                    product = 1
                    for c in self.args[n]:
                        product *= memo[c]
                    memo[n] = product
        return memo[node]

    def evaluate_many(
        self, nodes: Iterable[int], excluded: frozenset[int] = frozenset()
    ) -> dict[int, int]:
        """Evaluate several roots sharing one memo (one pass over the DAG)."""
        roots = list(nodes)
        memo: dict[int, int] = {}
        for root in roots:
            if root in memo:
                continue
            stack: list[tuple[int, bool]] = [(root, False)]
            while stack:
                n, ready = stack.pop()
                if n in memo:
                    continue
                kind = self.kinds[n]
                if kind == NODE_ONE:
                    memo[n] = 1
                elif kind == NODE_ZERO:
                    memo[n] = 0
                elif kind == NODE_TOKEN:
                    memo[n] = 0 if self.args[n][0] in excluded else 1
                else:
                    if not ready:
                        stack.append((n, True))
                        for c in self.args[n]:
                            if c not in memo:
                                stack.append((c, False))
                        continue
                    if kind == NODE_ADD:
                        memo[n] = sum(memo[c] for c in self.args[n])
                    else:
                        product = 1
                        for c in self.args[n]:
                            product *= memo[c]
                        memo[n] = product
        return {root: memo[root] for root in roots}

    # -- serialisation ------------------------------------------------------

    def encode(self) -> dict:
        nodes = []
        for i, (kind, args) in enumerate(zip(self.kinds, self.args)):
            if kind == NODE_TOKEN:
                nodes.append(["token", args[0]])
            elif kind == NODE_ONE:
                nodes.append(["one"])
            elif kind == NODE_ZERO:
                nodes.append(["zero"])
            elif kind == NODE_ADD:
                nodes.append(["add", *args])
            else:
                nodes.append(["mul", *args])
        return {"root_one": self._one, "root_zero": self._zero, "nodes": nodes}

    @classmethod
    def decode(cls, obj: dict, universe: Universe) -> "ProvenanceGraph":
        graph = cls.__new__(cls)
        graph.universe = universe
        graph.kinds = []
        graph.args = []
        graph._intern = {}
        for spec in obj["nodes"]:
            kind = spec[0]
            if kind == "token":
                graph.kinds.append(NODE_TOKEN)
                graph.args.append((int(spec[1]),))
            elif kind in ("one", "zero"):
                graph.kinds.append(kind)
                graph.args.append(())
            else:
                graph.kinds.append(kind)
                graph.args.append(tuple(int(x) for x in spec[1:]))
        graph._one = int(obj["root_one"])
        graph._zero = int(obj["root_zero"])
        return graph

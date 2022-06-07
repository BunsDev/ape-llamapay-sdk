from decimal import Decimal
from functools import cached_property, singledispatch, singledispatchmethod
from typing import List, Literal, Optional

from ape.api import ReceiptAPI
from ape.types import AddressType, ContractLog
from ape.utils import ManagerAccessMixin
from ape_tokens import tokens
from ape_tokens.managers import ERC20
from eth_abi.packed import encode_abi_packed
from eth_utils import keccak
from dataclasses import dataclass

from llamapay.constants import CONTRACT_TYPES, DURATION_TO_SECONDS, FACTORY_DEPLOYMENTS, PRECISION
from llamapay.exceptions import PoolNotDeployed


class Factory(ManagerAccessMixin):
    """
    LlamaPay streams for each token are contained in pools.
    This factory helps discover and deploy new pools.
    """

    def __init__(self):
        self.deployment = FACTORY_DEPLOYMENTS.get(
            ecosystem=self.provider.network.ecosystem.name,
            network=self.provider.network.name.replace("-fork", ""),
        )
        self.contract = self.create_contract(
            self.deployment.address, CONTRACT_TYPES["LlamaPayFactory"]
        )

    def get_pool(self, token: str) -> "Pool":
        """
        Get pool by token address or symbol.
        """
        token = self._resolve_token(token)
        address, is_deployed = self.contract.getLlamaPayContractByToken(token)
        if not is_deployed:
            raise PoolNotDeployed("deterministic address: %s" % address)

        return Pool(address, factory=self)

    def create_pool(self, token: str, **tx_args) -> "Pool":
        """
        Create a pool for a token and return it.
        """
        token = self._resolve_token(token)
        self.contract.createLlamaPayContract(token, **tx_args)

        return self.get_pool(token)

    def create_stream(self, recipient: AddressType, rate: str, token: Optional[str] = None):
        """
        >>> factory.create_stream('hentai.eth', '1000 DAI/month')
        """

    @property
    def pools(self) -> List["Pool"]:
        """
        Get all pools deployed by a factory.
        """
        # TODO update to use multicall
        pool_count = self.contract.getLlamaPayContractCount()
        pools = [
            Pool(self.contract.getLlamaPayContractByIndex(i), factory=self)
            for i in range(pool_count)
        ]
        return pools

    def _resolve_token(self, token: str) -> AddressType:
        """
        Resolve token address by symbol, address or ENS.
        """
        try:
            token = tokens[token].address
        except KeyError:
            pass

        return self.conversion_manager.convert(token, AddressType)


class Pool(ManagerAccessMixin):
    """
    A pool handles all streams for a specific token.
    """

    def __init__(self, address: AddressType, factory: Factory):
        self.address = address
        self.factory = factory
        self.contract = self.create_contract(
            self.address,
            CONTRACT_TYPES["LlamaPay"],  # type: ignore
        )
        self.token = self.create_contract(self.contract.token(), ERC20)
        # cache
        self._logs: List[ContractLog] = []
        self._last_logs_block = self.factory.deployment.deploy_block
        self._streams: List["Stream"] = []

    @cached_property
    def symbol(self):
        return self.token.symbol()

    @cached_property
    def scale(self):
        return 10 ** self.token.decimals()

    def _refresh_logs(self):
        start = self._last_logs_block
        head = self.chain_manager.blocks.height
        if start >= head:
            return

        logs = list(
            self.provider.get_contract_logs(
                self.address,
                self.contract.contract_type.events,
                start_block=start,
                stop_block=head,
                block_page_size=10_000,
            )
        )
        self._last_logs_block = head + 1
        self._logs.extend(logs)

        for log in logs:
            if log.name in ["StreamCreated", "StreamCreatedWithReason", "StreamModified"]:
                self._streams.append(
                    Stream(
                        source=log.event_arguments["from"],
                        target=log.to,
                        rate=log.amountPerSec,
                        pool=self,
                    )
                )

    @property
    def all_streams(self) -> List["Stream"]:
        self._refresh_logs()
        return self._streams[:]

    def find_streams(
        self,
        *,
        source: Optional[AddressType] = None,
        target: Optional[AddressType] = None,
    ) -> List["Stream"]:
        # handle ens
        if source:
            source = self.conversion_manager.convert(source, AddressType)
        if target:
            target = self.conversion_manager.convert(target, AddressType)
        # source & target
        if source and target:
            return [s for s in self.all_streams if s.source == source and s.target == target]
        elif source:
            return [s for s in self.all_streams if s.source == source]
        elif target:
            return [s for s in self.all_streams if s.target == target]
        else:
            raise ValueError("must specify source or target")

    def get_balance(self, payer: AddressType) -> Decimal:
        return Decimal(self.contract.getPayerBalance(payer)) / self.scale

    def approve(self, amount: Optional[Decimal] = None, **tx_args) -> ReceiptAPI:
        """
        Approve token to be deposited into a pool.

        Arguments:
            amount: decimal amount in tokens [default: infinite]
        """
        amount = 2**256 - 1 if amount is None else amount * self.scale
        return self.token.approve(self.address, amount, **tx_args)

    def deposit(self, amount: Decimal, **tx_args) -> ReceiptAPI:
        """
        Deposit funding balance into a pool.

        Arguments:
            amount: decimal amount in tokens
        """
        if self.token.allowance(tx_args["sender"], self.address) < amount:
            self.approve(amount, **tx_args)

        return self.contract.deposit(amount * self.scale, **tx_args)

    def withdraw(self, amount: Optional[Decimal] = None, **tx_args) -> ReceiptAPI:
        """
        Withdraw funding balance from a pool.

        Arguments:
            amount: decimal amount in tokens [default: withdraw all]
        """
        if amount:
            return self.contract.withdrawPayer(amount * self.scale, **tx_args)
        else:
            return self.contract.withdrawPayerAll(**tx_args)

    def __repr__(self):
        return f"<Pool address={self.address} token={self.symbol}>"

    def __eq__(self, other) -> bool:
        assert isinstance(other, Pool)
        return self.address == other.address


@dataclass
class Stream:
    """
    Represents a payment stream.
    """

    source: str
    target: str
    rate: int  # rate in tokens per second, scaled to 1e20, doesn't depend of token decimals
    pool: Pool

    @property
    def id(self) -> bytes:
        return keccak(
            encode_abi_packed(
                ["address", "address", "uint216"],
                [self.source, self.target, self.rate],
            )
        )

    def create(self, **tx_args):
        return self.pool.contract.createStream(self.target, self.rate, **tx_args)

    def pause(self, **tx_args):
        return self.pool.contract.pauseStream(self.target, self.rate, **tx_args)

    def cancel(self, **tx_args):
        return self.pool.contract.pauseStream(self.target, self.rate, **tx_args)

    def replace(self, stream: "Stream", **tx_args):
        return self.pool.contract.modifyStream(
            self.target, self.rate, stream.target, stream.rate, **tx_args
        )

    def send(self, **tx_args):
        """
        Push the pending amount to a target. Can be called by anyone.
        """
        return self.pool.contract.withdraw(self.source, self.target, self.rate, **tx_args)

    @property
    def balance(self):
        """
        Withdrawable balance of a stream.
        """
        result = self.pool.contract.withdrawable(self.source, self.target, self.rate)
        return Decimal(result.withdrawableAmount) / self.pool.scale

    class Config:
        arbitrary_types_allowed = True


@dataclass
class Rate:
    amount: Decimal
    period: Literal["day", "week", "month", "year"]
    token: Optional[str] = None
    raw_value: Optional[int] = None

    @property
    def per_sec(self):
        # this is the amount you feed to llamapay contracts
        return self.raw_value or int(self.amount * PRECISION / DURATION_TO_SECONDS[self.period])

    @singledispatchmethod
    @classmethod
    def parse(cls, rate):
        raise TypeError("unsupported rate type")

    @parse.register
    @classmethod
    def parse_int(cls, rate: int):
        per_month = Decimal(rate) * DURATION_TO_SECONDS["month"] / PRECISION
        return cls(
            amount=per_month,
            period="month",
            raw_value=rate,
        )

    @parse.register
    @classmethod
    def parse_str(cls, rate: str):
        amount, period = rate.split("/")
        assert period in DURATION_TO_SECONDS

        try:
            amount, token = amount.split(maxsplit=2)
        except ValueError:
            amount, token = amount, None

        amount = amount.replace(",", "_")

        return cls(amount=Decimal(amount), period=period, token=token)
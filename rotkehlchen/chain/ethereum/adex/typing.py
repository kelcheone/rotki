from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple, Union

from eth_typing.evm import ChecksumAddress
from eth_utils.typing import HexStr

from rotkehlchen.accounting.structures import Balance
from rotkehlchen.fval import FVal
from rotkehlchen.typing import Timestamp

# Pools data
TOM_POOL_ID = HexStr('0x2ce0c96383fb229d9776f33846e983a956a7d95844fac57b180ed0071d93bb28')
POOL_ID_POOL_NAME = {
    TOM_POOL_ID: 'Tom',
}

AdexEventDBTuple = (
    Tuple[
        str,  # tx_hash
        str,  # address
        str,  # identity_address
        int,  # timestamp
        str,  # type
        str,  # pool_id
        str,  # amount
        str,  # usd_value
        Optional[str],  # bond_id
        Optional[int],  # nonce
        Optional[int],  # slashed_at
        Optional[int],  # unlock_at
        Optional[str],  # channel_id
    ]
)


class EventType(Enum):
    """Supported events"""
    BOND = 1
    UNBOND = 2
    UNBOND_REQUEST = 3
    CHANNEL_WITHDRAW = 4

    def __str__(self) -> str:
        if self == EventType.BOND:
            return 'deposit'
        if self == EventType.UNBOND:
            return 'withdraw'
        if self == EventType.UNBOND_REQUEST:
            return 'withdraw request'
        if self == EventType.CHANNEL_WITHDRAW:
            return 'claim'
        raise AttributeError(f'Corrupt value {self} for EventType -- Should never happen')


@dataclass(init=True, repr=True)
class Bond:
    tx_hash: HexStr  # from bond.id
    address: ChecksumAddress
    identity_address: ChecksumAddress
    timestamp: Timestamp
    bond_id: HexStr
    pool_id: HexStr
    value: Balance
    nonce: int
    slashed_at: Timestamp  # from bond.slashedAtStart

    def serialize(self) -> Dict[str, Any]:
        return {
            'tx_hash': self.tx_hash,
            'identity_address': self.identity_address,
            'timestamp': self.timestamp,
            'bond_id': self.bond_id,
            'pool_id': self.pool_id,
            'pool_name': POOL_ID_POOL_NAME.get(self.pool_id, None),
            'value': self.value.serialize(),
            'event_type': str(EventType.BOND),
        }

    def to_db_tuple(self) -> AdexEventDBTuple:
        return (
            str(self.tx_hash),
            str(self.address),
            str(self.identity_address),
            int(self.timestamp),
            str(EventType.BOND),
            str(self.pool_id),
            str(self.value.amount),
            str(self.value.usd_value),
            str(self.bond_id),
            self.nonce,
            int(self.slashed_at),
            None,  # unlock_at
            None,  # channel_id
        )


@dataclass(init=True, repr=True)
class Unbond:
    tx_hash: HexStr  # from unbond.id
    address: ChecksumAddress
    identity_address: ChecksumAddress
    timestamp: Timestamp
    bond_id: HexStr
    value: Balance  # from bond.amount
    pool_id: HexStr = HexStr('')  # from bond.pool_id

    def serialize(self) -> Dict[str, Any]:
        return {
            'tx_hash': self.tx_hash,
            'identity_address': self.identity_address,
            'timestamp': self.timestamp,
            'bond_id': self.bond_id,
            'pool_id': self.pool_id,
            'pool_name': POOL_ID_POOL_NAME.get(self.pool_id, None),
            'value': self.value.serialize(),
            'event_type': str(EventType.UNBOND),
        }

    def to_db_tuple(self) -> AdexEventDBTuple:
        return (
            str(self.tx_hash),
            str(self.address),
            str(self.identity_address),
            int(self.timestamp),
            str(EventType.UNBOND),
            str(self.pool_id),
            str(self.value.amount),
            str(self.value.usd_value),
            str(self.bond_id),
            None,  # nonce
            None,  # slashed_at
            None,  # unlock_at
            None,  # channel_id
        )


@dataclass(init=True, repr=True)
class UnbondRequest:
    tx_hash: HexStr  # from unbond.id
    address: ChecksumAddress
    identity_address: ChecksumAddress
    timestamp: Timestamp
    bond_id: HexStr
    unlock_at: Timestamp  # from unbondRequest.willUnlock
    value: Balance  # from bond.amount
    pool_id: HexStr = HexStr('')  # from bond.pool_id

    def serialize(self) -> Dict[str, Any]:
        return {
            'tx_hash': self.tx_hash,
            'identity_address': self.identity_address,
            'timestamp': self.timestamp,
            'bond_id': self.bond_id,
            'pool_id': self.pool_id,
            'pool_name': POOL_ID_POOL_NAME.get(self.pool_id, None),
            'value': self.value.serialize(),
            'event_type': str(EventType.UNBOND_REQUEST),
        }

    def to_db_tuple(self) -> AdexEventDBTuple:
        return (
            str(self.tx_hash),
            str(self.address),
            str(self.identity_address),
            int(self.timestamp),
            str(EventType.UNBOND_REQUEST),
            str(self.pool_id),
            str(self.value.amount),
            str(self.value.usd_value),
            str(self.bond_id),
            None,  # nonce
            None,  # slashed_at
            int(self.unlock_at),
            None,  # channel_id
        )


@dataclass(init=True, repr=True)
class ChannelWithdraw:
    tx_hash: HexStr  # from channelWithdraw.id
    address: ChecksumAddress
    identity_address: ChecksumAddress
    timestamp: Timestamp
    value: Balance
    channel_id: HexStr
    pool_id: HexStr

    def serialize(self) -> Dict[str, Any]:
        return {
            'tx_hash': self.tx_hash,
            'identity_address': self.identity_address,
            'timestamp': self.timestamp,
            'pool_id': self.pool_id,
            'pool_name': POOL_ID_POOL_NAME.get(self.pool_id, None),
            'value': self.value.serialize(),
            'event_type': str(EventType.CHANNEL_WITHDRAW),
        }

    def to_db_tuple(self) -> AdexEventDBTuple:
        return (
            str(self.tx_hash),
            str(self.address),
            str(self.identity_address),
            int(self.timestamp),
            str(EventType.CHANNEL_WITHDRAW),
            str(self.pool_id),
            str(self.value.amount),
            str(self.value.usd_value),
            None,  # bond_id
            None,  # nonce
            None,  # slashed_at
            None,  # unlocked_at
            str(self.channel_id),
        )


# Contains the events' (e.g. bond, unbond) common attributes
class EventCoreData(NamedTuple):
    tx_hash: HexStr
    address: ChecksumAddress
    identity_address: ChecksumAddress
    timestamp: Timestamp


class ADXStakingEvents(NamedTuple):
    bonds: List[Bond]
    unbonds: List[Unbond]
    unbond_requests: List[UnbondRequest]
    channel_withdraws: List[ChannelWithdraw]


class UnclaimedReward(NamedTuple):
    adx_amount: FVal
    dai_amount: FVal


class ADXStakingBalance(NamedTuple):
    pool_id: HexStr
    pool_name: Optional[str]
    adx_balance: Balance
    adx_unclaimed_balance: Balance
    dai_balance: Balance
    contract_address: ChecksumAddress  # From staking contract

    def serialize(self) -> Dict[str, Any]:
        return {
            'pool_id': self.pool_id,
            'pool_name': self.pool_name,
            'adx_balance': self.adx_balance.serialize(),
            'dai_balance': self.dai_balance.serialize(),
            'contract_address': self.contract_address,
        }


class TomPoolIncentive(NamedTuple):
    total_staked_amount: FVal  # from sum(currentTotalActiveStake)
    total_reward_per_second: FVal  # from sum(currentRewardPerSecond)
    period_ends_at: Timestamp  # from periodEnd
    apr: FVal  # from AdEx APY


class ADXStakingDetail(NamedTuple):
    contract_address: ChecksumAddress  # From staking contract
    pool_id: HexStr
    pool_name: Optional[str]
    total_staked_amount: FVal
    apr: FVal
    adx_balance: Balance
    dai_balance: Balance
    profit_loss: Balance

    def serialize(self) -> Dict[str, Any]:
        return {
            'contract_address': self.contract_address,
            'pool_id': self.pool_id,
            'pool_name': self.pool_name,
            'total_staked_amount': str(self.total_staked_amount),
            'apr': self.apr.to_percentage(precision=2),
            'adx_balance': self.adx_balance.serialize(),
            'dai_balance': self.dai_balance.serialize(),
            'profit_loss': self.profit_loss.serialize(),
        }


class ADXStakingHistory(NamedTuple):
    events: List[Union[Bond, Unbond, UnbondRequest]]
    staking_details: List[ADXStakingDetail]

    def serialize(self) -> Dict[str, Any]:
        return {
            'events': [event.serialize() for event in self.events],
            'staking_details': [detail.serialize() for detail in self.staking_details],
        }


DeserializationMethod = Callable[..., Union[Bond, Unbond, UnbondRequest, ChannelWithdraw]]
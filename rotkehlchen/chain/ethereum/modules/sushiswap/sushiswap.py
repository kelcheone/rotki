import logging
from collections import defaultdict
from datetime import datetime, time
from typing import TYPE_CHECKING, List, Optional, Set

from rotkehlchen.accounting.structures import Balance
from rotkehlchen.assets.asset import EthereumToken
from rotkehlchen.assets.utils import get_or_create_ethereum_token
from rotkehlchen.chain.ethereum.graph import GRAPH_QUERY_LIMIT, Graph, format_query_indentation
from rotkehlchen.chain.ethereum.modules.ammswap.ammswap import AMMSwapPlatform
from rotkehlchen.chain.ethereum.modules.ammswap.typing import (
    AddressEvents,
    AddressEventsBalances,
    AddressToLPBalances,
    AddressTrades,
    AssetToPrice,
    DDAddressEvents,
    DDAddressToLPBalances,
    EventType,
    LiquidityPool,
    LiquidityPoolAsset,
    ProtocolBalance,
)
from rotkehlchen.chain.ethereum.modules.ammswap.utils import SUBGRAPH_REMOTE_ERROR_MSG
from rotkehlchen.chain.ethereum.trades import AMMSwap, AMMTrade
from rotkehlchen.errors import DeserializationError, ModuleInitializationFailure, RemoteError
from rotkehlchen.fval import FVal
from rotkehlchen.premium.premium import Premium
from rotkehlchen.serialization.deserialize import deserialize_ethereum_address
from rotkehlchen.typing import (
    AssetAmount,
    ChecksumEthAddress,
    Location,
    Price,
    Timestamp,
)
from rotkehlchen.user_messages import MessagesAggregator
from rotkehlchen.utils.interfaces import EthereumModule

from .graph import (
    LIQUIDITY_POSITIONS_QUERY,
    SWAPS_QUERY,
    TOKEN_DAY_DATAS_QUERY,
)
from .utils import get_latest_lp_addresses, uniswap_lp_token_balances

if TYPE_CHECKING:
    from rotkehlchen.chain.ethereum.manager import EthereumManager
    from rotkehlchen.db.dbhandler import DBHandler

log = logging.getLogger(__name__)

SUSHISWAP_EVENTS_PREFIX = 'sushiswap_events'
SUSHISWAP_TRADES_PREFIX = 'sushiswap_trades'


class Sushiswap(AMMSwapPlatform, EthereumModule):
    """Sushiswap integration module

    * Sushiswap subgraph:
    https://github.com/sushiswap/sushiswap-subgraph
    """
    def __init__(
            self,
            ethereum_manager: 'EthereumManager',
            database: 'DBHandler',
            premium: Optional[Premium],
            msg_aggregator: MessagesAggregator,
    ) -> None:
        super().__init__(
            ethereum_manager=ethereum_manager,
            database=database,
            premium=premium,
            msg_aggregator=msg_aggregator,
        )
        self.location = Location.SUSHISWAP
        try:
            self.graph = Graph(
                'https://api.thegraph.com/subgraphs/name/sushiswap/exchange',
            )
        except RemoteError as e:
            self.msg_aggregator.add_error(SUBGRAPH_REMOTE_ERROR_MSG.format(error_msg=str(e)))
            raise ModuleInitializationFailure('subgraph remote error') from e

    def _get_balances_graph(
            self,
            addresses: List[ChecksumEthAddress],
    ) -> ProtocolBalance:
        """Get the addresses' pools data querying the Sushiswap subgraph

        Each liquidity position is converted into a <LiquidityPool>.
        """
        address_balances: DDAddressToLPBalances = defaultdict(list)
        known_assets: Set[EthereumToken] = set()
        unknown_assets: Set[EthereumToken] = set()

        addresses_lower = [address.lower() for address in addresses]
        querystr = format_query_indentation(LIQUIDITY_POSITIONS_QUERY.format())
        param_types = {
            '$limit': 'Int!',
            '$offset': 'Int!',
            '$addresses': '[String!]',
            '$balance': 'BigDecimal!',
        }
        param_values = {
            'limit': GRAPH_QUERY_LIMIT,
            'offset': 0,
            'addresses': addresses_lower,
            'balance': '0',
        }
        while True:
            try:
                result = self.graph.query(
                    querystr=querystr,
                    param_types=param_types,
                    param_values=param_values,
                )
            except RemoteError as e:
                self.msg_aggregator.add_error(SUBGRAPH_REMOTE_ERROR_MSG.format(error_msg=str(e)))
                raise

            result_data = result['liquidityPositions']

            for lp in result_data:
                lp_pair = lp['pair']
                lp_total_supply = FVal(lp_pair['totalSupply'])
                user_lp_balance = FVal(lp['liquidityTokenBalance'])
                try:
                    user_address = deserialize_ethereum_address(lp['user']['id'])
                    lp_address = deserialize_ethereum_address(lp_pair['id'])
                except DeserializationError as e:
                    msg = (
                        f'Failed to Deserialize address. Skipping pool {lp_pair}'
                        f'with user address {lp["user"]["id"]}'
                    )
                    log.error(msg)
                    raise RemoteError(msg) from e

                # Insert LP tokens reserves within tokens dicts
                token0 = lp_pair['token0']
                token0['total_amount'] = lp_pair['reserve0']
                token1 = lp_pair['token1']
                token1['total_amount'] = lp_pair['reserve1']

                liquidity_pool_assets = []

                for token in token0, token1:
                    try:
                        deserialized_eth_address = deserialize_ethereum_address(token['id'])
                    except DeserializationError as e:
                        msg = (
                            f'Failed to deserialize token address {token["id"]}'
                            f'Bad token address in lp pair came from the graph.'
                        )
                        log.error(msg)
                        raise RemoteError(msg) from e

                    asset = get_or_create_ethereum_token(
                        userdb=self.database,
                        symbol=token['symbol'],
                        ethereum_address=deserialized_eth_address,
                        name=token['name'],
                        decimals=int(token['decimals']),
                    )
                    if asset.has_oracle():
                        known_assets.add(asset)
                    else:
                        unknown_assets.add(asset)

                    # Estimate the underlying asset total_amount
                    asset_total_amount = FVal(token['total_amount'])
                    user_asset_balance = (
                        user_lp_balance / lp_total_supply * asset_total_amount
                    )

                    liquidity_pool_asset = LiquidityPoolAsset(
                        asset=asset,
                        total_amount=asset_total_amount,
                        user_balance=Balance(amount=user_asset_balance),
                    )
                    liquidity_pool_assets.append(liquidity_pool_asset)

                liquidity_pool = LiquidityPool(
                    address=lp_address,
                    assets=liquidity_pool_assets,
                    total_supply=lp_total_supply,
                    user_balance=Balance(amount=user_lp_balance),
                )
                address_balances[user_address].append(liquidity_pool)

            # Check whether an extra request is needed
            if len(result_data) < GRAPH_QUERY_LIMIT:
                break

            # Update pagination step
            param_values = {
                **param_values,
                'offset': param_values['offset'] + GRAPH_QUERY_LIMIT,  # type: ignore
            }

        protocol_balance = ProtocolBalance(
            address_balances=dict(address_balances),
            known_assets=known_assets,
            unknown_assets=unknown_assets,
        )
        return protocol_balance

    def get_balances_chain(self, addresses: List[ChecksumEthAddress]) -> ProtocolBalance:
        """Get the addresses' pools data via chain queries.
        """
        known_assets: Set[EthereumToken] = set()
        unknown_assets: Set[EthereumToken] = set()
        lp_addresses = get_latest_lp_addresses(self.data_directory)

        address_mapping = {}
        for address in addresses:
            pool_balances = uniswap_lp_token_balances(
                userdb=self.database,
                address=address,
                ethereum=self.ethereum,
                lp_addresses=lp_addresses,
                known_assets=known_assets,
                unknown_assets=unknown_assets,
            )
            if len(pool_balances) != 0:
                address_mapping[address] = pool_balances

        protocol_balance = ProtocolBalance(
            address_balances=address_mapping,
            known_assets=known_assets,
            unknown_assets=unknown_assets,
        )
        return protocol_balance

    def _get_events_balances(
            self,
            addresses: List[ChecksumEthAddress],
            from_timestamp: Timestamp,
            to_timestamp: Timestamp,
    ) -> AddressEventsBalances:
        """Request via graph all events for new addresses and the latest ones
        for already existing addresses. Then the requested events are written
        in DB and finally all DB events are read, and processed for calculating
        total profit/loss per LP (stored within <LiquidityPoolEventsBalance>).
        """
        address_events_balances: AddressEventsBalances = {}
        address_events: DDAddressEvents = defaultdict(list)
        db_address_events: AddressEvents = {}
        new_addresses: List[ChecksumEthAddress] = []
        existing_addresses: List[ChecksumEthAddress] = []
        min_end_ts: Timestamp = to_timestamp

        # Get addresses' last used query range for Sushiswap events
        for address in addresses:
            entry_name = f'{SUSHISWAP_EVENTS_PREFIX}_{address}'
            events_range = self.database.get_used_query_range(name=entry_name)

            if not events_range:
                new_addresses.append(address)
            else:
                existing_addresses.append(address)
                min_end_ts = min(min_end_ts, events_range[1])

        # Request new addresses' events
        if new_addresses:
            start_ts = Timestamp(0)
            for address in new_addresses:
                for event_type in EventType:
                    new_address_events = self._get_events_graph(
                        address=address,
                        start_ts=start_ts,
                        end_ts=to_timestamp,
                        event_type=event_type,
                    )
                    if new_address_events:
                        address_events[address].extend(new_address_events)

                # Insert new address' last used query range
                self.database.update_used_query_range(
                    name=f'{SUSHISWAP_EVENTS_PREFIX}_{address}',
                    start_ts=start_ts,
                    end_ts=to_timestamp,
                )

        # Request existing DB addresses' events
        if existing_addresses and to_timestamp > min_end_ts:
            for address in existing_addresses:
                for event_type in EventType:
                    address_new_events = self._get_events_graph(
                        address=address,
                        start_ts=min_end_ts,
                        end_ts=to_timestamp,
                        event_type=event_type,
                    )
                    if address_new_events:
                        address_events[address].extend(address_new_events)

                # Update existing address' last used query range
                self.database.update_used_query_range(
                    name=f'{SUSHISWAP_EVENTS_PREFIX}_{address}',
                    start_ts=min_end_ts,
                    end_ts=to_timestamp,
                )

        # Insert requested events in DB
        all_events = []
        for address in filter(lambda address: address in address_events, addresses):
            all_events.extend(address_events[address])

        self.database.add_sushiswap_events(all_events)

        # Fetch all DB events within the time range
        for address in addresses:
            db_events = self.database.get_sushiswap_events(
                from_ts=from_timestamp,
                to_ts=to_timestamp,
                address=address,
            )
            if db_events:
                # return events with the oldest first
                db_events.sort(key=lambda event: (event.timestamp, event.log_index))
                db_address_events[address] = db_events

        # Request addresses' current balances (UNI-V2s and underlying tokens)
        # if there is no specific time range in this endpoint call (i.e. all
        # events). Current balances in the protocol are needed for an accurate
        # profit/loss calculation.
        # TODO: when this endpoint is called with a specific time range,
        # getting the balances and underlying tokens within that time range
        # requires an archive node. Feature pending to be developed.
        address_balances: AddressToLPBalances = {}  # Empty when specific time range
        if from_timestamp == Timestamp(0):
            address_balances = self.get_balances(addresses)

        # Calculate addresses' event balances (i.e. profit/loss per pool)
        for address, events in db_address_events.items():
            balances = address_balances.get(address, [])  # Empty when specific time range
            events_balances = self._calculate_events_balances(
                address=address,
                events=events,
                balances=balances,
            )
            address_events_balances[address] = events_balances

        return address_events_balances

    def _get_trades(
            self,
            addresses: List[ChecksumEthAddress],
            from_timestamp: Timestamp,
            to_timestamp: Timestamp,
            only_cache: bool,
    ) -> AddressTrades:
        """Request via graph all trades for new addresses and the latest ones
        for already existing addresses. Then the requested trade are written in
        DB and finally all DB trades are read and returned.
        """
        address_amm_trades: AddressTrades = {}
        new_addresses: List[ChecksumEthAddress] = []
        existing_addresses: List[ChecksumEthAddress] = []
        min_end_ts: Timestamp = to_timestamp

        if only_cache:
            return self._fetch_trades_from_db(addresses, from_timestamp, to_timestamp)

        # Get addresses' last used query range for Sushiswap trades
        for address in addresses:
            entry_name = f'{SUSHISWAP_TRADES_PREFIX}_{address}'
            trades_range = self.database.get_used_query_range(name=entry_name)

            if not trades_range:
                new_addresses.append(address)
            else:
                existing_addresses.append(address)
                min_end_ts = min(min_end_ts, trades_range[1])

        # Request new addresses' trades
        if new_addresses:
            start_ts = Timestamp(0)
            new_address_trades = self._get_trades_graph(
                addresses=new_addresses,
                start_ts=start_ts,
                end_ts=to_timestamp,
            )
            address_amm_trades.update(new_address_trades)

            # Insert last used query range for new addresses
            for address in new_addresses:
                entry_name = f'{SUSHISWAP_TRADES_PREFIX}_{address}'
                self.database.update_used_query_range(
                    name=entry_name,
                    start_ts=start_ts,
                    end_ts=to_timestamp,
                )

        # Request existing DB addresses' trades
        if existing_addresses and to_timestamp > min_end_ts:
            address_new_trades = self._get_trades_graph(
                addresses=existing_addresses,
                start_ts=min_end_ts,
                end_ts=to_timestamp,
            )
            address_amm_trades.update(address_new_trades)

            # Update last used query range for existing addresses
            for address in existing_addresses:
                entry_name = f'{SUSHISWAP_TRADES_PREFIX}_{address}'
                self.database.update_used_query_range(
                    name=entry_name,
                    start_ts=min_end_ts,
                    end_ts=to_timestamp,
                )

        # Insert all unique swaps to the D
        all_swaps = set()
        for address in filter(lambda address: address in address_amm_trades, addresses):
            for trade in address_amm_trades[address]:
                for swap in trade.swaps:
                    all_swaps.add(swap)

        self.database.add_amm_swaps(list(all_swaps))
        return self._fetch_trades_from_db(addresses, from_timestamp, to_timestamp)

    def _get_trades_graph_for_address(
            self,
            address: ChecksumEthAddress,
            start_ts: Timestamp,
            end_ts: Timestamp,
    ) -> List[AMMTrade]:
        """Get the address' trades data querying the Sushiswap subgraph

        Each trade (swap) instantiates an <AMMTrade>.

        The trade pair (i.e. BASE_QUOTE) is determined by `reserve0_reserve1`.
        Translated to Sushiswap lingo:

        Trade type BUY:
        - `asset1In` (QUOTE, reserve1) is gt 0.
        - `asset0Out` (BASE, reserve0) is gt 0.

        Trade type SELL:
        - `asset0In` (BASE, reserve0) is gt 0.
        - `asset1Out` (QUOTE, reserve1) is gt 0.

        May raise
        - RemoteError
        """
        trades: List[AMMTrade] = []
        param_types = {
            '$limit': 'Int!',
            '$offset': 'Int!',
            '$address': 'Bytes!',
            '$start_ts': 'BigInt!',
            '$end_ts': 'BigInt!',
        }
        param_values = {
            'limit': GRAPH_QUERY_LIMIT,
            'offset': 0,
            'address': address.lower(),
            'start_ts': str(start_ts),
            'end_ts': str(end_ts),
        }
        querystr = format_query_indentation(SWAPS_QUERY.format())

        while True:
            try:
                result = self.graph.query(
                    querystr=querystr,
                    param_types=param_types,
                    param_values=param_values,
                )
            except RemoteError as e:
                self.msg_aggregator.add_error(SUBGRAPH_REMOTE_ERROR_MSG.format(error_msg=str(e)))
                break

            for entry in result['swaps']:
                swaps = []
                for swap in entry['transaction']['swaps']:
                    timestamp = swap['timestamp']
                    swap_token0 = swap['pair']['token0']
                    swap_token1 = swap['pair']['token1']

                    try:
                        token0_deserialized = deserialize_ethereum_address(swap_token0['id'])
                        token1_deserialized = deserialize_ethereum_address(swap_token1['id'])
                        from_address_deserialized = deserialize_ethereum_address(swap['sender'])
                        to_address_deserialized = deserialize_ethereum_address(swap['to'])
                    except DeserializationError:
                        msg = (
                            f'Failed to deserialize addresses in trade from sushiswap graph with '
                            f'token 0: {swap_token0["id"]}, token 1: {swap_token1["id"]}, '
                            f'swap sender: {swap["sender"]}, swap receiver {swap["to"]}'
                        )
                        log.error(msg)
                        continue

                    token0 = get_or_create_ethereum_token(
                        userdb=self.database,
                        symbol=swap_token0['symbol'],
                        ethereum_address=token0_deserialized,
                        name=swap_token0['name'],
                        decimals=swap_token0['decimals'],
                    )
                    token1 = get_or_create_ethereum_token(
                        userdb=self.database,
                        symbol=swap_token1['symbol'],
                        ethereum_address=token1_deserialized,
                        name=swap_token1['name'],
                        decimals=int(swap_token1['decimals']),
                    )

                    try:
                        amount0_in = FVal(swap['amount0In'])
                        amount1_in = FVal(swap['amount1In'])
                        amount0_out = FVal(swap['amount0Out'])
                        amount1_out = FVal(swap['amount1Out'])
                    except ValueError as e:
                        log.error(
                            f'Failed to read amounts in sushiswap V2 swap {str(swap)}. '
                            f'{str(e)}.',
                        )
                        continue

                    swaps.append(AMMSwap(
                        tx_hash=swap['id'].split('-')[0],
                        log_index=int(swap['logIndex']),
                        address=address,
                        from_address=from_address_deserialized,
                        to_address=to_address_deserialized,
                        timestamp=Timestamp(int(timestamp)),
                        location=self.location,
                        token0=token0,
                        token1=token1,
                        amount0_in=AssetAmount(amount0_in),
                        amount1_in=AssetAmount(amount1_in),
                        amount0_out=AssetAmount(amount0_out),
                        amount1_out=AssetAmount(amount1_out),
                    ))

                # with the new logic the list of swaps can be empty, in that case don't try
                # to make trades from the swaps
                if len(swaps) == 0:
                    continue

                # Now that we got all swaps for a transaction, create the trade object
                trades.extend(self._tx_swaps_to_trades(swaps))

            # Check whether an extra request is needed
            if len(result['swaps']) < GRAPH_QUERY_LIMIT:
                break

            # Update pagination step
            param_values = {
                **param_values,
                'offset': param_values['offset'] + GRAPH_QUERY_LIMIT,  # type: ignore
            }
        return trades

    def _get_unknown_asset_price_graph(
            self,
            unknown_assets: Set[EthereumToken],
    ) -> AssetToPrice:
        """Get today's tokens prices via the Sushiswap subgraph

        Sushiswap provides a token price every day at 00:00:00 UTC
        This function can raise RemoteError
        """
        asset_price: AssetToPrice = {}

        unknown_assets_addresses = (
            [asset.ethereum_address.lower() for asset in unknown_assets]
        )
        querystr = format_query_indentation(TOKEN_DAY_DATAS_QUERY.format())
        today_epoch = int(
            datetime.combine(datetime.utcnow().date(), time.min).timestamp(),
        )
        param_types = {
            '$limit': 'Int!',
            '$offset': 'Int!',
            '$token_ids': '[String!]',
            '$datetime': 'Int!',
        }
        param_values = {
            'limit': GRAPH_QUERY_LIMIT,
            'offset': 0,
            'token_ids': unknown_assets_addresses,
            'datetime': today_epoch,
        }
        while True:
            try:
                result = self.graph.query(
                    querystr=querystr,
                    param_types=param_types,
                    param_values=param_values,
                )
            except RemoteError as e:
                self.msg_aggregator.add_error(SUBGRAPH_REMOTE_ERROR_MSG.format(error_msg=str(e)))
                raise

            result_data = result['tokenDayDatas']

            for tdd in result_data:
                try:
                    token_address = deserialize_ethereum_address(tdd['token']['id'])
                except DeserializationError as e:
                    msg = (
                        f'Error deserializing address {tdd["token"]["id"]} '
                        f'during sushiswap prices query from graph.'
                    )
                    log.error(msg)
                    raise RemoteError(msg) from e
                asset_price[token_address] = Price(FVal(tdd['priceUSD']))

            # Check whether an extra request is needed
            if len(result_data) < GRAPH_QUERY_LIMIT:
                break

            # Update pagination step
            param_values = {
                **param_values,
                'offset': param_values['offset'] + GRAPH_QUERY_LIMIT,  # type: ignore
            }

        return asset_price

    def get_balances(
        self,
        addresses: List[ChecksumEthAddress],
    ) -> AddressToLPBalances:
        """Get the addresses' balances in the Sushiswap protocol

        Premium users can request balances either via the Sushiswap subgraph or
        on-chain.
        """
        if self.premium:
            protocol_balance = self._get_balances_graph(addresses=addresses)
        else:
            protocol_balance = self.get_balances_chain(addresses)

        known_assets = protocol_balance.known_assets
        unknown_assets = protocol_balance.unknown_assets

        known_asset_price = self._get_known_asset_price(
            known_assets=known_assets,
            unknown_assets=unknown_assets,
        )

        unknown_asset_price: AssetToPrice = {}
        if self.premium:
            unknown_asset_price = self._get_unknown_asset_price_graph(unknown_assets=unknown_assets)  # noqa:E501

        self._update_assets_prices_in_address_balances(
            address_balances=protocol_balance.address_balances,
            known_asset_price=known_asset_price,
            unknown_asset_price=unknown_asset_price,
        )

        return protocol_balance.address_balances

    def get_trades_history(
        self,
        addresses: List[ChecksumEthAddress],
        reset_db_data: bool,
        from_timestamp: Timestamp,
        to_timestamp: Timestamp,
    ) -> AddressTrades:
        """Get the addresses' trades history in the Sushiswap protocol
        """
        with self.trades_lock:
            if reset_db_data is True:
                self.database.delete_sushiswap_trades_data()

            trades = self._get_trades(
                addresses=addresses,
                from_timestamp=from_timestamp,
                to_timestamp=to_timestamp,
                only_cache=False,
            )

        return trades

    def deactivate(self) -> None:
        self.database.delete_sushiswap_trades_data()
        self.database.delete_sushiswap_events_data()

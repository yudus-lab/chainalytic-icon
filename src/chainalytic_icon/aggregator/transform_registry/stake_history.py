import json
import time
from typing import Dict, List, Optional, Set, Tuple, Union

from iconservice.icon_config import default_icon_config
from iconservice.icon_constant import ConfigKey
from iconservice.iiss.engine import Engine

from chainalytic_icon.aggregator.transform import BaseTransform


def unlock_period(total_stake, total_supply):
    p = Engine._calculate_unstake_lock_period(
        default_icon_config[ConfigKey.IISS_META_DATA][ConfigKey.UN_STAKE_LOCK_MIN],
        default_icon_config[ConfigKey.IISS_META_DATA][ConfigKey.UN_STAKE_LOCK_MAX],
        default_icon_config[ConfigKey.IISS_META_DATA][ConfigKey.REWARD_POINT],
        total_stake,
        total_supply,
    )

    return p


class Transform(BaseTransform):
    START_BLOCK_HEIGHT = FIRST_STAKE_BLOCK_HEIGHT = 7597365 # This is used in Aggregator service initialization

    LAST_STATE_HEIGHT_KEY = b'last_state_height'
    LAST_TOTAL_STAKING_KEY = b'last_total_staking'
    LAST_TOTAL_UNSTAKING_KEY = b'last_total_unstaking'
    LAST_TOTAL_STAKING_WALLETS_KEY = b'last_total_staking_wallets'
    LAST_TOTAL_UNSTAKING_WALLETS_KEY = b'last_total_unstaking_wallets'

    def __init__(self, working_dir: str, transform_id: str):
        super(Transform, self).__init__(working_dir, transform_id)

    async def execute(self, height: int, input_data: dict) -> Optional[Dict]:

        cache_db = self.transform_cache_db
        cache_db_batch = self.transform_cache_db.write_batch()

        if not self.ensure_block_height_match(height):
            return self.load_last_output()

        # #################################################

        # Read data of previous block from cache
        #
        prev_total_staking = cache_db.get(Transform.LAST_TOTAL_STAKING_KEY)
        prev_total_unstaking = cache_db.get(Transform.LAST_TOTAL_UNSTAKING_KEY)
        prev_total_staking_wallets = cache_db.get(Transform.LAST_TOTAL_STAKING_WALLETS_KEY)
        prev_total_unstaking_wallets = cache_db.get(Transform.LAST_TOTAL_UNSTAKING_WALLETS_KEY)

        prev_total_staking = float(prev_total_staking) if prev_total_staking else 0
        prev_total_unstaking = float(prev_total_unstaking) if prev_total_unstaking else 0
        prev_total_staking_wallets = (
            int(float(prev_total_staking_wallets)) if prev_total_staking_wallets else 0
        )
        prev_total_unstaking_wallets = (
            int(float(prev_total_unstaking_wallets)) if prev_total_unstaking_wallets else 0
        )

        total_staking = prev_total_staking
        total_unstaking = prev_total_unstaking
        total_staking_wallets = prev_total_staking_wallets
        total_unstaking_wallets = prev_total_unstaking_wallets

        unstake_state_changed = 0

        # Cleanup expired unlock period
        #
        unstaking_addresses = cache_db.get(b'unstaking')
        if unstaking_addresses:
            unstaking_addresses = json.loads(unstaking_addresses)
        else:
            unstaking_addresses = {}
            unstake_state_changed = 1
        for addr in list(unstaking_addresses):
            stake_value, unstaking_value, request_height, unlock_height = unstaking_addresses[
                addr
            ].split(':')
            if int(unlock_height) <= height:
                unstaking_addresses.pop(addr)
                cache_db_batch.put(addr.encode(), f'{stake_value}:0:0:0'.encode())
                unstake_state_changed = 1

        cache_db_batch.put(b'unstaking', json.dumps(unstaking_addresses).encode())

        # Calculate staking, unstaking and unlock_height for each wallet
        # and put them to transform cache
        # Only process wallets that set new stake in current block
        #
        set_stake_wallets = input_data['data']
        timestamp = input_data['timestamp']
        total_supply = input_data['total_supply']

        for addr in set_stake_wallets:
            addr_data = cache_db.get(addr.encode())

            if addr_data:
                (
                    prev_stake_value,
                    prev_unstaking_value,
                    request_height,
                    unlock_height,
                ) = addr_data.split(b':')
                prev_stake_value = float(prev_stake_value)
                prev_unstaking_value = float(prev_unstaking_value)
                request_height = int(request_height)
                unlock_height = int(unlock_height)
            else:
                prev_stake_value = prev_unstaking_value = request_height = unlock_height = 0

            cur_stake_value = round(set_stake_wallets[addr], 4)
            cur_unstaking_value = 0

            if prev_stake_value == 0 and cur_stake_value > 0:
                total_staking_wallets += 1
            elif prev_stake_value > 0 and cur_stake_value == 0:
                total_staking_wallets -= 1

            # Unstake
            if cur_stake_value < prev_stake_value:
                if prev_unstaking_value > 0:
                    cur_unstaking_value = prev_unstaking_value + (
                        prev_stake_value - cur_stake_value
                    )

                else:
                    cur_unstaking_value = prev_stake_value - cur_stake_value
                unlock_height = height + unlock_period(prev_total_staking, total_supply)
                request_height = height

            # Restake
            else:
                if prev_unstaking_value > 0:
                    cur_unstaking_value = prev_unstaking_value - (
                        cur_stake_value - prev_stake_value
                    )
                else:
                    cur_unstaking_value = 0

            if cur_unstaking_value <= 0:
                cur_unstaking_value = 0
                unlock_height = 0
                request_height = 0

            cache_db_batch.put(
                addr.encode(),
                f'{cur_stake_value}:{cur_unstaking_value}:{request_height}:{unlock_height}'.encode(),
            )

            # Update unstaking wallets list
            if cur_unstaking_value > 0:
                unstaking_addresses[
                    addr
                ] = f'{cur_stake_value}:{cur_unstaking_value}:{request_height}:{unlock_height}'
                unstake_state_changed = 1
            elif addr in unstaking_addresses:
                unstaking_addresses.pop(addr)
                unstake_state_changed = 1

            cache_db_batch.put(b'unstaking', json.dumps(unstaking_addresses).encode())

            # Update total staking
            total_staking = total_staking - prev_stake_value + cur_stake_value

        # Update total unstaking wallets
        total_unstaking_wallets = len(unstaking_addresses)

        # Calculate latest total unstaking
        total_unstaking = 0
        for addr in unstaking_addresses:
            stake_value, unstaking_value, request_height, unlock_height = unstaking_addresses[
                addr
            ].split(':')
            total_unstaking += float(unstaking_value)

        cache_db_batch.put(Transform.LAST_STATE_HEIGHT_KEY, str(height).encode())
        cache_db_batch.put(Transform.LAST_TOTAL_STAKING_KEY, str(total_staking).encode())
        cache_db_batch.put(Transform.LAST_TOTAL_UNSTAKING_KEY, str(total_unstaking).encode())
        cache_db_batch.put(
            Transform.LAST_TOTAL_STAKING_WALLETS_KEY, str(total_staking_wallets).encode()
        )
        cache_db_batch.put(
            Transform.LAST_TOTAL_UNSTAKING_WALLETS_KEY, str(total_unstaking_wallets).encode()
        )
        cache_db_batch.write()

        data = {
            'total_staking': total_staking,
            'total_unstaking': total_unstaking,
            'total_staking_wallets': total_staking_wallets,
            'total_unstaking_wallets': total_unstaking_wallets,
            'timestamp': timestamp,
        }

        output = {
            'height': height,
            'block_data': data,
            'latest_state_data': {
                'latest_unstake_state': {
                    'wallets': unstaking_addresses if unstake_state_changed else None,
                    'height': height,
                }
            },
        }

        self.save_last_output(output)

        return output

import json
import time
from typing import Dict, List, Optional, Set, Tuple, Union

import plyvel
from iconservice.icon_config import default_icon_config
from iconservice.icon_constant import ConfigKey
from iconservice.iiss.engine import Engine

from chainalytic_icon.aggregator.transform import BaseTransform


class Transform(BaseTransform):
    START_BLOCK_HEIGHT = 19452235

    CONTRACT_LIST_KEY = b'contract_list'
    TX_KEY = b'tx'
    INTERNAL_TX_KEY = b'internal_tx'
    LAST_STATE_HEIGHT_KEY = b'last_state_height'

    def __init__(self, working_dir: str, transform_id: str):
        super(Transform, self).__init__(working_dir, transform_id)

    async def execute(self, height: int, input_data: dict) -> Optional[Dict]:
        start_time = time.time()

        # Load transform cache to retrive previous staking state
        cache_db = self.transform_cache_db
        cache_db_batch = self.transform_cache_db.write_batch()

        # Make sure input block data represents for the next block of previous state cache
        prev_state_height = cache_db.get(Transform.LAST_STATE_HEIGHT_KEY)
        if prev_state_height:
            prev_state_height = int(prev_state_height)
            if prev_state_height != height - 1:
                return None

        # #################################################

        contract_txs = input_data['data']
        updated_contracts = {}

        for tx in contract_txs:
            addr = tx['contract_address']
            if addr not in updated_contracts:
                updated_contracts[addr] = {
                    'stats': {},
                    'tx': {},
                    'internal_tx': {},
                }

            if not updated_contracts[addr]['stats']:
                updated_contracts[addr]['stats'] = cache_db.get(addr.encode())
                if not updated_contracts[addr]['stats']:
                    updated_contracts[addr]['stats'] = {
                        'tx_volume': 0,
                        'tx_count': 0,
                        'internal_tx_volume': 0,
                        'internal_tx_count': 0,
                    }
                else:
                    updated_contracts[addr]['stats'] = json.loads(updated_contracts[addr]['stats'])

            if tx['internal_tx_target']:
                updated_contracts[addr]['stats']['internal_tx_count'] += 1
                next_tx_id = updated_contracts[addr]['stats']['internal_tx_count']
                updated_contracts[addr]['internal_tx'][f'{next_tx_id}'] = {
                    'height': height,
                    'timestamp': tx['timestamp'],
                    'hash': tx['hash'],
                    'value': tx['value'],
                    'fee': tx['fee'],
                    'internal_tx_target': tx['internal_tx_target'],
                    'internal_tx_value': tx['internal_tx_value'],
                }
                if tx['internal_tx_value']:
                    updated_contracts[addr]['stats']['internal_tx_volume'] += tx[
                        'internal_tx_value'
                    ]
            else:
                updated_contracts[addr]['stats']['tx_count'] += 1
                next_tx_id = updated_contracts[addr]['stats']['tx_count']
                updated_contracts[addr]['tx'][f'{next_tx_id}'] = {
                    'height': height,
                    'timestamp': tx['timestamp'],
                    'hash': tx['hash'],
                    'from': tx['from'],
                    'value': tx['value'],
                    'fee': tx['fee'],
                }
                if tx['value']:
                    updated_contracts[addr]['stats']['tx_volume'] += tx['value']

            cache_db_batch.put(addr.encode(), json.dumps(updated_contracts[addr]['stats']).encode())

        cache_db_batch.put(Transform.LAST_STATE_HEIGHT_KEY, str(height).encode())
        cache_db_batch.write()

        return {
            'height': height,
            'data': {},
            'misc': {
                'updated_contract_state': {'updated_contracts': updated_contracts, 'height': height}
            },
        }

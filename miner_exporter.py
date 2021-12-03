#!/usr/bin/env python3

# external packages
import prometheus_client
import requests
import psutil

# internal packages
import datetime
import time
import os
import logging
import typing
from miner_jsonrpc import MinerJSONRPC

# remember, levels: debug, info, warning, error, critical. there is no trace.
logging.basicConfig(format="%(filename)s:%(funcName)s:%(lineno)d:%(levelname)s\t%(message)s", level=logging.WARNING)
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

# get options
UPDATE_PERIOD = int(os.environ.get('UPDATE_PERIOD', 30))
MINER_EXPORTER_PORT = int(os.environ.get('MINER_EXPORTER_PORT', 9825)) # 9-VAL on your phone
VALIDATOR_JSONRPC_ADDRESS = os.environ.get('VALIDATOR_JSONRPC_ADDRESS', 'http://localhost:4467/')
COLLECT_SYSTEM_USAGE = os.environ.get('COLLECT_SYSTEM_USAGE', "").lower() in ("true", "t", "1", "y", "yes")

# gather metrics for all validators, instead of just "this" validator. This is a large
# collection, so plan accordingly.
ALL_HBBFT = os.environ.get('ALL_HBBFT', "").lower() in ("true", "t", "1", "y", "yes")
ALL_PENALTIES = os.environ.get('ALL_PENALTIES', "").lower() in ("true", "t", "1", "y", "yes")

# prometheus exporter types Gauge,Counter,Summary,Histogram,Info and Enum
SCRAPE_TIME = prometheus_client.Summary('validator_scrape_time', 'Time spent collecting miner data')
CHAIN_STATS = prometheus_client.Gauge('chain_stats',
                              'Stats about the global chain', ['resource_type'])
VAL = prometheus_client.Gauge('validator_height',
                              "Height of the validator's blockchain",
                              ['resource_type','validator_name'])
INCON = prometheus_client.Gauge('validator_inconsensus',
                              'Is validator currently in consensus group',
                              ['validator_name'])
BLOCKAGE = prometheus_client.Gauge('validator_block_age',
                              'Age of the current block',
                             ['validator_name'])
HEARTBEAT = prometheus_client.Gauge('validator_last_heartbeat',
                              'Blocks since last validator heartbeat',
                             ['validator_name','validator_address'])
HBBFT_PERF = prometheus_client.Gauge('validator_hbbft_perf',
                              'HBBFT performance metrics from perf, only applies when in CG',
                             ['resource_type','subtype','validator_name'])
CONNECTIONS = prometheus_client.Gauge('validator_connections',
                              'Number of libp2p connections ',
                             ['resource_type','validator_name'])
SESSIONS = prometheus_client.Gauge('validator_sessions',
                              'Number of libp2p sessions',
                             ['resource_type','validator_name'])
LEDGER_PENALTY = prometheus_client.Gauge('validator_ledger',
                              'Validator performance metrics ',
                             ['resource_type', 'subtype','validator_name', 'validator_address'])
VALIDATOR_VERSION = prometheus_client.Info('validator_version',
                              'Version number of the miner container',['validator_name'],)
BALANCE = prometheus_client.Gauge('account_balance',
                              'Balance of the validator owner account',['validator_name'])
if COLLECT_SYSTEM_USAGE:
    SYSTEM_USAGE = prometheus_client.Gauge('system_usage',
                                           'Hold current system resource usage',
                                           ['resource_type','hostname'])

# hostname of machine for use in system stats
hostname = os.uname()[1]

# Decorate function with metric.
@SCRAPE_TIME.time()
def stats(miner: MinerJSONRPC):
    try:
        addr = miner.addr()
    except:
        # This is a non-recoverable error, so many things
        # depend on knowing the address that it's silly
        # to attempt to proceed without it.
        log.error("can't get validator's address")
        return

    try:
        name = miner.name()
    except:
        # This is a non-recoverable error, so many things
        # depend on knowing the address that it's silly
        # to attempt to proceed without it.
        log.error("can't get validator's name")
        return

    if COLLECT_SYSTEM_USAGE:
        # collect total cpu and memory usage. Might want to consider just the docker
        # container with something like cadvisor instead
        SYSTEM_USAGE.labels('CPU', hostname).set(psutil.cpu_percent())
        SYSTEM_USAGE.labels('Memory', hostname).set(psutil.virtual_memory()[2])
        SYSTEM_USAGE.labels('CPU-Steal', hostname).set(psutil.cpu_times_percent().steal)
        SYSTEM_USAGE.labels('Disk Used', hostname).set(float(psutil.disk_usage('/').used) / float(psutil.disk_usage('/').total))
        SYSTEM_USAGE.labels('Disk Free', hostname).set(float(psutil.disk_usage('/').free) / float(psutil.disk_usage('/').total))
        SYSTEM_USAGE.labels('Process-Count', hostname).set(sum(1 for proc in psutil.process_iter()))

    #
    # Safely try to obtain as many items as possible.
    #
    height_info = None
    try:
        height_info = miner.info_height()
    except:
        log.error("chain height fetch failure")

    in_consensus = None
    try:
        in_consensus = miner.in_consensus()
    except:
        log.error("in consensus fetch failure")

    penalty_ledger = None
    try:
        if ALL_PENALTIES:
            penalty_ledger = miner.ledger_validators()
            # Turn a list of dicts into a dict of dicts, indexed on the angry-purple-tiger.
            # While doing this, keep only staked validators.
            penalty_ledger = {v['address']:v for v in miner.ledger_validators() if v['status'] == 'staked'}
        else:
            penalty_ledger = {addr: miner.ledger_validators(address=addr)}

    except:
        log.error("validator fetch failure")

    owner = None
    if penalty_ledger is not None and addr in penalty_ledger:
        owner = penalty_ledger[addr]['owner_address']

    balance = None
    if owner is not None:
        try:
            balance_result = miner.ledger_balance(address=owner)
            balance = balance_result['balance'] / 1.0e8
        except:
            log.error("owner balance fetch failure")

    version = None
    try:
        version = miner.version()
    except:
        log.error("version fetch error")

    block_age = None
    try:
        block_age = miner.block_age()
    except:
        log.error("block age fetch failure")

    hbbft_perf = None
    try:
        hbbft_perf = miner.hbbft_perf()
    except:
        log.error("hbbft perf fetch failure")

    peer_book_info = None
    try:
        peer_book_info = miner.peer_book_self()
    except:
        log.error("peer book self fetch failure")

    #
    # Parse results, update gauges.
    #

    if height_info is not None:
        # If `sync_height` is present then the validator is
        # syncing and behind, otherwise it is in sync.
        chain_height = height_info['height']
        val_height = height_info.get('sync_height', chain_height)

        VAL.labels('Height', name).set(val_height)
        CHAIN_STATS.labels('Height').set(chain_height)

    if in_consensus is not None:
        INCON.labels(name).set(in_consensus)

    if balance is not None:
        BALANCE.labels(name).set(balance)

    if block_age is not None:
        BLOCKAGE.labels(name).set(block_age)

    if version is not None:
        VALIDATOR_VERSION.labels(name).info({'version':version})

    if penalty_ledger is not None:
        # Clear metric so unstaked validators drop off
        LEDGER_PENALTY.clear()

        for address, ledger_entry in penalty_ledger.items():
            LEDGER_PENALTY.labels('ledger_penalties', 'tenure', ledger_entry['name'], ledger_entry['address']).set(ledger_entry['tenure_penalty'])
            LEDGER_PENALTY.labels('ledger_penalties', 'dkg', ledger_entry['name'], ledger_entry['address']).set(ledger_entry['dkg_penalty'])
            LEDGER_PENALTY.labels('ledger_penalties', 'performance', ledger_entry['name'], ledger_entry['address']).set(ledger_entry['performance_penalty'])
            LEDGER_PENALTY.labels('ledger_penalties', 'total', ledger_entry['name'], ledger_entry['address']).set(ledger_entry['total_penalty'])
            if ledger_entry['tenure_penalty'] > 0:
                LEDGER_PENALTY.labels('ledger_penalties', 'perf_tenure_ratio', ledger_entry['name'], ledger_entry['address']).set((ledger_entry['performance_penalty'] + ledger_entry['dkg_penalty'])/ledger_entry['tenure_penalty'])
            HEARTBEAT.labels(ledger_entry['name'], ledger_entry['address']).set(ledger_entry['last_heartbeat'])

    if hbbft_perf is not None:
        # Clear metric so non-CG members drop off
        HBBFT_PERF.clear()

        # Values common to all members of the CG
        bba_tot = hbbft_perf['blocks_since_epoch']
        seen_tot = hbbft_perf['max_seen']

        for member in hbbft_perf['consensus_members']:
            if member['address'] == addr or ALL_HBBFT:
                HBBFT_PERF.labels('hbbft_perf','Penalty', member['name']).set(member['penalty'])
                HBBFT_PERF.labels('hbbft_perf','BBA_Total', member['name']).set(bba_tot)
                HBBFT_PERF.labels('hbbft_perf','BBA_Votes', member['name']).set(member['bba_completions'])
                HBBFT_PERF.labels('hbbft_perf','Seen_Total', member['name']).set(seen_tot)
                HBBFT_PERF.labels('hbbft_perf','Seen_Votes', member['name']).set(member['seen_votes'])
                HBBFT_PERF.labels('hbbft_perf','BBA_Last', member['name']).set(member['last_bba'])
                HBBFT_PERF.labels('hbbft_perf','Seen_Last', member['name']).set(member['last_seen'])
                HBBFT_PERF.labels('hbbft_perf','Tenure', member['name']).set(member['tenure'])

    if peer_book_info is not None:
        connections = peer_book_info[0]['connection_count']
        CONNECTIONS.labels('connections', name).set(connections)
        sessions = len(peer_book_info[0]['sessions'])
        SESSIONS.labels('sessions', name).set(sessions)


if __name__ == '__main__':
  prometheus_client.start_http_server(MINER_EXPORTER_PORT)
  miner = MinerJSONRPC(VALIDATOR_JSONRPC_ADDRESS)
  while True:
    #log.warning("starting loop.")
    try:
      stats(miner)
    except ValueError as ex:
      log.error(f"stats loop failed.", exc_info=ex)

    # sleep
    time.sleep(UPDATE_PERIOD)


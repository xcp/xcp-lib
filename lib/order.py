#! /usr/bin/python3

import struct
import decimal
D = decimal.Decimal

from . import (util, config, exceptions, bitcoin, util)

FORMAT = '>QQQQHQ'
LENGTH = 8 + 8 + 8 + 8 + 2 + 8
ID = 10

def validate (db, source, give_asset, give_amount, get_asset, get_amount, expiration, fee_required, block_index):
    problems = []
    cursor = db.cursor()

    if give_asset == get_asset:
        problems.append('trading an asset for itself')

    if not isinstance(give_amount, int): problems.append('give_amount must be in satoshi')
    if not isinstance(get_amount, int): problems.append('get_amount must be in satoshi')
    if not isinstance(fee_required, int): problems.append('fee_required must be in satoshi')
    if not isinstance(expiration, int): problems.append('expiration must be expressed as an integer block delta')

    if give_amount <= 0: problems.append('non‐positive give quantity')
    if get_amount <= 0: problems.append('non‐positive get quantity')
    if fee_required < 0: problems.append('negative fee_required')
    if expiration <= 0: problems.append('non‐positive expiration')

    if not give_amount or not get_amount:
        problems.append('zero give or zero get')
    cursor.execute('select * from issuances where (status = ? and asset = ?)', ('valid', give_asset))
    if give_asset not in ('BTC', 'XCP') and not cursor.fetchall():
        problems.append('no such asset to give ({})'.format(give_asset))
    cursor.execute('select * from issuances where (status = ? and asset = ?)', ('valid', get_asset))
    if get_asset not in ('BTC', 'XCP') and not cursor.fetchall():
        problems.append('no such asset to get ({})'.format(get_asset))
    if expiration > config.MAX_EXPIRATION:
        problems.append('maximum expiration time exceeded')

    # For SQLite3
    if give_amount > config.MAX_INT or get_amount > config.MAX_INT or fee_required > config.MAX_INT:
        problems.append('maximum integer size exceeded')

    cursor.close()
    return problems

def compose (db, source, give_asset, give_amount, get_asset, get_amount, expiration, fee_required, fee_provided):
    balances = util.get_balances(db, address=source, asset=give_asset)
    if give_asset != 'BTC' and (not balances or balances[0]['amount'] < give_amount):
        raise exceptions.OrderError('insufficient funds')

    problems = validate(db, source, give_asset, give_amount, get_asset, get_amount, expiration, fee_required, None)
    if problems: raise exceptions.OrderError(problems)

    give_id = util.get_asset_id(give_asset)
    get_id = util.get_asset_id(get_asset)
    data = config.PREFIX + struct.pack(config.TXTYPE_FORMAT, ID)
    data += struct.pack(FORMAT, give_id, give_amount, get_id, get_amount,
                        expiration, fee_required)
    return (source, None, None, fee_provided, data)

def parse (db, tx, message):
    order_parse_cursor = db.cursor()

    # Unpack message.
    try:
        assert len(message) == LENGTH
        give_id, give_amount, get_id, get_amount, expiration, fee_required = struct.unpack(FORMAT, message)
        give_asset = util.get_asset_name(give_id)
        get_asset = util.get_asset_name(get_id)
        status = 'valid'
    except struct.error as e:
        give_asset, give_amount, get_asset, get_amount, expiration, fee_required = None, None, None, None, None, None
        status = 'invalid: could not unpack'

    price = 0
    if status == 'valid':
        try: price = D(get_amount) / D(give_amount)
        except: pass

        # Overorder
        order_parse_cursor.execute('''SELECT * FROM balances \
                                      WHERE (address = ? AND asset = ?)''', (tx['source'], give_asset))
        balances = order_parse_cursor.fetchall()
        if give_asset != 'BTC':
            if not balances:  give_amount = 0
            elif balances[0]['amount'] < give_amount:
                give_amount = min(balances[0]['amount'], give_amount)
                get_amount = int(price * D(give_amount))

        problems = validate(db, tx['source'], give_asset, give_amount, get_asset, get_amount, expiration, fee_required, tx['block_index'])
        if problems: status = 'invalid: ' + ';'.join(problems)

    if status == 'valid':
        if give_asset != 'BTC':  # No need (or way) to debit BTC.
            util.debit(db, tx['block_index'], tx['source'], give_asset, give_amount, event=tx['tx_hash'])

    # Add parsed transaction to message-type–specific table.
    bindings = {
        'tx_index': tx['tx_index'],
        'tx_hash': tx['tx_hash'],
        'block_index': tx['block_index'],
        'source': tx['source'],
        'give_asset': give_asset,
        'give_amount': give_amount,
        'give_remaining': give_amount,
        'get_asset': get_asset,
        'get_amount': get_amount,
        'get_remaining': get_amount,
        'expiration': expiration,
        'expire_index': tx['block_index'] + expiration,
        'fee_required': fee_required,
        'fee_required_remaining': fee_required,
        'fee_provided': tx['fee'],
        'fee_provided_remaining': tx['fee'],
        'status': status,
    }
    sql='insert into orders values(:tx_index, :tx_hash, :block_index, :source, :give_asset, :give_amount, :give_remaining, :get_asset, :get_amount, :get_remaining, :expiration, :expire_index, :fee_required, :fee_required_remaining, :fee_provided, :fee_provided_remaining, :status)'
    order_parse_cursor.execute(sql, bindings)

    # Match.
    match(db, tx)

    order_parse_cursor.close()

def match (db, tx):
    cursor = db.cursor()

    # Get order in question.
    orders = list(cursor.execute('''SELECT * FROM orders\
                                    WHERE tx_index=?''', (tx['tx_index'],)))
    assert len(orders) == 1
    tx1 = orders[0]

    cursor.execute('''SELECT * FROM orders \
                      WHERE (give_asset=? AND get_asset=? AND status=?)''',
                   (tx1['get_asset'], tx1['give_asset'], 'valid'))

    tx1_give_remaining = tx1['give_remaining']
    tx1_get_remaining = tx1['get_remaining']

    order_matches = cursor.fetchall()
    if tx['block_index'] > 284500 or config.TESTNET:  # Protocol change.
        order_matches = sorted(order_matches, key=lambda x: x['tx_index'])                              # Sort by tx index second.
        order_matches = sorted(order_matches, key=lambda x: D(x['get_amount']) / D(x['give_amount']))   # Sort by price first.

    # Get fee remaining.
    tx1_fee_required_remaining = tx1['fee_required_remaining']
    tx1_fee_provided_remaining = tx1['fee_provided_remaining']

    for tx0 in order_matches:
        tx0_give_remaining = tx0['give_remaining']
        tx0_get_remaining = tx0['get_remaining']

        # Get fee provided remaining.
        tx0_fee_required_remaining = tx0['fee_required_remaining']
        tx0_fee_provided_remaining = tx0['fee_provided_remaining']

        # Make sure that that both orders still have funds remaining [to be sold].
        if tx0_give_remaining <= 0 or tx1_give_remaining <= 0: continue

        # If the prices agree, make the trade. The found order sets the price,
        # and they trade as much as they can.
        tx0_price = util.price(tx0['get_amount'], tx0['give_amount'])
        tx1_price = util.price(tx1['get_amount'], tx1['give_amount'])
        tx1_inverse_price = util.price(tx1['give_amount'], tx1['get_amount'])

        # Protocol change.
        if tx['block_index'] < 286000: tx1_inverse_price = D(1) / tx1_price

        # print('foo', tx0_price, tx1_inverse_price) # TODO
        if tx0_price <= tx1_inverse_price:
            forward_amount = int(min(tx0_give_remaining, D(tx1_give_remaining) / tx0_price))
            backward_amount = round(forward_amount * tx0_price)

            if not forward_amount: continue
            if tx1['block_index'] >= 286500 or config.TESTNET:    # Protocol change.
                if not backward_amount: continue
            # print('bar', backward_amount) # TODO

            # Check and update fee remainings.
            if tx1['block_index'] >= 286500 or config.TESTNET: # Protocol change. Deduct fee_required from fee_provided_remaining, etc., if possible (else don’t match).
                if tx1['get_asset'] == 'BTC':
                    fee = int(D(tx1['fee_required_remaining']) * D(forward_amount) / D(tx1_get_remaining))
                    # print('baz', tx0_fee_provided_remaining, fee) # TODO
                    if tx0_fee_provided_remaining < fee: continue
                    else:
                        tx0_fee_provided_remaining -= fee
                        if tx1['block_index'] >= 287800 or config.TESTNET:  # Protocol change.
                            tx1_fee_required_remaining -= fee
                elif tx1['give_asset'] == 'BTC':
                    fee = int(D(tx0['fee_required_remaining']) * D(backward_amount) / D(tx0_get_remaining))
                    # print('qux', tx1_fee_provided_remaining, fee) # TODO
                    if tx1_fee_provided_remaining < fee: continue
                    else:
                        tx1_fee_provided_remaining -= fee 
                        if tx1['block_index'] >= 287800 or config.TESTNET:  # Protocol change.
                            tx0_fee_required_remaining -= fee
            else:   # Don’t deduct.
                if tx1['get_asset'] == 'BTC':
                    if tx0_fee_provided_remaining < tx1['fee_required']: continue
                elif tx1['give_asset'] == 'BTC':
                    if tx1_fee_provided_remaining < tx0['fee_required']: continue

            forward_asset, backward_asset = tx1['get_asset'], tx1['give_asset']
            order_match_id = tx0['tx_hash'] + tx1['tx_hash']

            if 'BTC' in (tx1['give_asset'], tx1['get_asset']):
                status = 'pending'
            else:
                status = 'completed'
                # Credit.
                util.credit(db, tx['block_index'], tx1['source'], tx1['get_asset'],
                                    forward_amount, event=order_match_id)
                util.credit(db, tx['block_index'], tx0['source'], tx0['get_asset'],
                                    backward_amount, event=order_match_id)

            # Debit the order, even if it involves giving bitcoins, and so one
            # can't debit the sending account.
            # Get remainings may be negative.
            tx0_give_remaining -= forward_amount
            tx0_get_remaining -= backward_amount
            tx1_give_remaining -= backward_amount
            tx1_get_remaining -= forward_amount

            # Update give_remaining, get_remaining.
            # tx0
            bindings = {
                'give_remaining': tx0_give_remaining,
                'get_remaining': tx0_get_remaining,
                'fee_required_remaining': tx0_fee_required_remaining,
                'fee_provided_remaining': tx0_fee_provided_remaining,
                'tx_index': tx0['tx_index']
            }
            sql='update orders set give_remaining = :give_remaining, get_remaining = :get_remaining, fee_required_remaining = :fee_required_remaining, fee_provided_remaining = :fee_provided_remaining where tx_index = :tx_index'
            cursor.execute(sql, bindings)
            # tx1
            bindings = {
                'give_remaining': tx1_give_remaining,
                'get_remaining': tx1_get_remaining,
                'fee_required_remaining': tx1_fee_required_remaining,
                'fee_provided_remaining': tx1_fee_provided_remaining,
                'tx_index': tx1['tx_index']
            }
            sql='update orders set give_remaining = :give_remaining, get_remaining = :get_remaining, fee_required_remaining = :fee_required_remaining, fee_provided_remaining = :fee_provided_remaining where tx_index = :tx_index'
            cursor.execute(sql, bindings)

            # Calculate when the match will expire.
            if tx1['block_index'] >= 286500 or config.TESTNET:    # Protocol change.
                match_expire_index = tx1['block_index'] + 10
            else:
                match_expire_index = min(tx0['expire_index'], tx1['expire_index'])

            # Record order match.
            bindings = {
                'id': tx0['tx_hash'] + tx['tx_hash'],
                'tx0_index': tx0['tx_index'],
                'tx0_hash': tx0['tx_hash'],
                'tx0_address': tx0['source'],
                'tx1_index': tx1['tx_index'],
                'tx1_hash': tx1['tx_hash'],
                'tx1_address': tx1['source'],
                'forward_asset': forward_asset,
                'forward_amount': forward_amount,
                'backward_asset': backward_asset,
                'backward_amount': backward_amount,
                'tx0_block_index': tx0['block_index'],
                'tx1_block_index': tx1['block_index'],
                'tx0_expiration': tx0['expiration'],
                'tx1_expiration': tx1['expiration'],
                'match_expire_index': match_expire_index,
                'status': status,
            }
            sql='insert into order_matches values(:id, :tx0_index, :tx0_hash, :tx0_address, :tx1_index, :tx1_hash, :tx1_address, :forward_asset, :forward_amount, :backward_asset, :backward_amount, :tx0_block_index, :tx1_block_index, :tx0_expiration, :tx1_expiration, :match_expire_index, :status)'
            cursor.execute(sql, bindings)

    cursor.close()

def expire (db, block_index):
    cursor = db.cursor()

    # Expire orders and give refunds for the amount give_remaining (if non-zero; if not BTC).
    cursor.execute('''SELECT * FROM orders \
                      WHERE (status = ? AND expire_index < ?)''', ('valid', block_index))
    for order in cursor.fetchall():

        # Update status of order.
        bindings = {
            'status': 'expired',
            'tx_index': order['tx_index']
        }
        sql='update orders set status = :status where tx_index = :tx_index'
        cursor.execute(sql, bindings)

        if order['give_asset'] != 'BTC':    # Can't credit BTC.
            util.credit(db, block_index, order['source'], order['give_asset'], order['give_remaining'], event=order['tx_hash'])

        # Record offer expiration.
        bindings = {
            'order_index': order['tx_index'],
            'order_hash': order['tx_hash'],
            'source': order['source'],
            'block_index': block_index
        }
        sql='insert into order_expirations values(:order_index, :order_hash, :source, :block_index)'
        cursor.execute(sql, bindings)

    # Expire order_matches for BTC with no BTC.
    cursor.execute('''SELECT * FROM order_matches \
                      WHERE (status = ? and match_expire_index < ?)''', ('pending', block_index))
    for order_match in cursor.fetchall():
        
        # Update status of order match.
        bindings = {
            'status': 'expired',
            'order_match_id': order_match['id']
        }
        sql='update order_matches set status = :status where id = :order_match_id'
        cursor.execute(sql, bindings)

        order_match_id = order_match['tx0_hash'] + order_match['tx1_hash']

        # Record order match expiration.
        bindings = {
            'order_match_id': order_match_id,
            'tx0_address': order_match['tx0_address'],
            'tx1_address': order_match['tx1_address'],
            'block_index': block_index
        }
        sql='insert into order_match_expirations values(:order_match_id, :tx0_address, :tx1_address, :block_index)'
        cursor.execute(sql, bindings)

        # If tx0 is still good, replenish give, get remaining.
        orders = list(cursor.execute('''SELECT * FROM orders \
                                        WHERE tx_index = ?''',
                                     (order_match['tx0_index'],)))
        assert len(orders) == 1
        tx0_order = orders[0]
        tx0_order_time_left = tx0_order['expire_index'] - block_index
        if tx0_order_time_left >= 0:
            bindings = {
                'give_remaining': tx0_order['give_remaining'] + order_match['forward_amount'],
                'get_remaining': tx0_order['get_remaining'] + order_match['backward_amount'],
                'tx_index': order_match['tx0_index']
            }
            sql='update orders set give_remaining = :give_remaining, get_remaining = :get_remaining where tx_index = :tx_index'
            cursor.execute(sql, bindings)
        # If tx0 is expired, credit address directly.
        elif order_match['forward_asset'] != 'BTC':
            util.credit(db, block_index, order_match['tx0_address'],
                        order_match['forward_asset'],
                        order_match['forward_amount'], event=order_match['id'])

        # If tx1 is still good, replenish give, get remaining.
        orders = list(cursor.execute('''SELECT * FROM orders \
                                        WHERE tx_index = ?''',
                                     (order_match['tx1_index'],)))
        assert len(orders) == 1
        tx1_order = orders[0]
        tx1_order_time_left = tx1_order['expire_index'] - block_index
        if tx1_order_time_left >= 0:
            bindings = {
                'give_remaining': tx1_order['give_remaining'] + order_match['backward_amount'],
                'get_remaining': tx1_order['get_remaining'] + order_match['forward_amount'],
                'tx_index': order_match['tx1_index']
            }
            sql='update orders set give_remaining = :give_remaining, get_remaining = :get_remaining where tx_index = :tx_index'
            cursor.execute(sql, bindings)
        # If tx1 is expired, credit address directly.
        elif order_match['backward_asset'] != 'BTC':
            util.credit(db, block_index, order_match['tx1_address'],
                        order_match['backward_asset'],
                        order_match['backward_amount'], event=order_match['id'])

        if block_index < 286500:    # Protocol change.
            # Sanity check: one of the two must have expired.
            assert tx0_order_time_left or tx1_order_time_left

    cursor.close()

# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4

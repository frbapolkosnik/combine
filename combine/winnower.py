#!/usr/bin/env python
# -*- coding: utf-8 -*-
import ConfigParser
import csv
import datetime as dt
import json
import os
import re
from logging import getLogger

import dns.resolver
import dns.reversename
import dnsdb_query
import pygeoip
from netaddr import IPAddress
from netaddr import IPRange
from netaddr import IPSet
from sortedcontainers import SortedDict

import uniaccept

logger = getLogger('winnower')

# from http://en.wikipedia.org/wiki/Reserved_IP_addresses:
reserved_ranges = IPSet(['0.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '192.88.99.0/24',
                         '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24', '233.252.0.0/24'])
gi_org = SortedDict()
geo_data = pygeoip.GeoIP('data/GeoIP.dat', pygeoip.MEMORY_CACHE)


def load_gi_org(filename):
    with open(filename, 'rb') as f:
        org_reader = csv.DictReader(f, fieldnames=['start', 'end', 'org'])
        for row in org_reader:
            gi_org[row['start']] = (IPRange(row['start'], row['end']), unicode(row['org'], errors='replace'))

    return gi_org


def org_by_addr(address):
    as_num = None
    as_name = None
    gi_index = gi_org.bisect(str(int(address)))
    gi_net = gi_org[gi_org.iloc[gi_index - 1]]
    if address in gi_net[0]:
        as_num, sep, as_name = gi_net[1].partition(' ')
        as_num = as_num.replace("AS", "")  # Making sure the variable only has the number
    return as_num, as_name


def maxhits(dns_records):
    hmax = 0
    hostname = None
    for record in dns_records:
        # logger.info("examining %s" % record)
        if record['count'] > hmax:
            hmax = record['count']
            hostname = record['rrname'].rstrip('.')
    return hostname


def enrich_IPv4(address, geo_data, dnsdb=None):
    try:
        result = {}
        result['as_num'], result['as_name'] = org_by_addr(address)
        result['country'] = geo_data.country_code_by_addr('%s' % address)
        hostname = None
        if dnsdb:
            result['dnsdb'] = maxhits(dnsdb.query_rdata_ip('%s' % address))
        a = dns.reversename.from_address(address)
        hostname = dns.resolver.query(a, "PTR")[0].to_text()
        if hostname:
            result['hostname'] = hostname
        return {'enriched': result}
    except Exception, e:
    	logger.error('enrich_IPv4: enrich address %s fails with error %s' % (address, e))
	return {'enriched': {} }

def enrich_FQDN(address, date, dnsdb=None):
    try:
        result = {}
        ip_addr = ''
        if dnsdb:
            records = dnsdb.query_rrset(address, rrtype='A')
            records = filter_date(records, date)
            ip_addr = maxhits(records)
            result['dnsdb'] = ip_addr
        mx = []
        answers = dns.resolver.query(address, 'MX')
        for rdata in answers:
            mx.append(str(rdata.exchange))
        if len(mx) > 0:
            result['MX'] = mx
        a = []
        answers = dns.resolver.query(address, 'A')
        for rdata in answers:
            a.append(str(rdata.address))
        if len(a) > 0:
            result['A'] = a
        return {'enriched': result}
    except Exception, e:
    	logger.error('enrich_FQDN: enrich address %s fails with error %s' % (address, e))
	return {'enriched': {} }

def enrich_hash(hash):
    # TODO something useful here
    return {'enriched': {}}


def filter_date(records, date):
    date_dt = dt.datetime.strptime(date, '%Y-%m-%d')
    start_dt = dt.datetime.combine(date_dt, dt.time.min).strftime('%Y-%m-%d %H:%M:%S')
    end_dt = dt.datetime.combine(date_dt, dt.time.max).strftime('%Y-%m-%d %H:%M:%S')
    return dnsdb_query.filter_before(dnsdb_query.filter_after(records, start_dt), end_dt)


def reserved(address):
    a_reserved = address.is_reserved()
    a_private = address.is_private()
    a_inr = address in reserved_ranges
    if a_reserved or a_private or a_inr:
        return True
    else:
        return False


def is_ipv4(address):
    try:
    	ip = IPAddress(address)
	if ip.version == 4:
	    return True
    except:
        return False


def is_ipv6(address):
    try:
    	ip = IPAddress(address)
	if ip.version == 6:
	    return True
    except:
        return False


def winnow(in_file, out_file, enr_file):
    config = ConfigParser.SafeConfigParser(allow_no_value=True)
    cfg_success = config.read('combine.cfg')
    if not cfg_success:
        logger.error('Winnower: Could not read combine.cfg.')
        logger.error('HINT: edit combine-example.cfg and save as combine.cfg.')
        return

    if not os.path.isfile('./tld-list.txt'):
        uniaccept.refreshtlddb("./tld-list.txt")

    server = config.get('Winnower', 'dnsdb_server')
    api = config.get('Winnower', 'dnsdb_api')
    enrich_ip = config.getboolean('Winnower', 'enrich_ip')
    if enrich_ip:
        logger.info('Enriching IPv4 indicators: TRUE')
    else:
        logger.info('Enriching IPv4 indicators: FALSE')

    enrich_dns = config.getboolean('Winnower', 'enrich_dns')
    if enrich_dns:
        logger.info('Enriching DNS indicators: TRUE')
    else:
        logger.info('Enriching DNS indicators: FALSE')

    enrich_hash = config.getboolean('Winnower', 'enrich_hash')
    if enrich_hash:
        logger.info('Enriching Hash indicators: TRUE')
    else:
        logger.info('Enriching Hash indicators: FALSE')

    logger.info('Setting up DNSDB client')

    # handle the case where we aren't using DNSDB
    dnsdb = dnsdb_query.DnsdbClient(server, api)
    if api == 'YOUR_API_KEY_HERE' or len(dnsdb.query_rdata_name('google.com')) == 0:
        dnsdb = None
        logger.info('Invalid DNSDB configuration found')

    with open(in_file, 'rb') as f:
        crop = json.load(f)

    plugin_dir = config.get('Thresher', 'plugin_directory')
    if plugin_dir is None or plugin_dir == '':
        logger.error("Thresher: Couldn't find plugins for processing")
        return

    gi_org_loc = config.get('Winnower', 'gi_org_loc')
    if gi_org_loc is None or gi_org_loc == '':
        gi_org_loc = 'data/GeoIPASNum2.csv'
    gi_data_loc = config.get('Winnower', 'gi_data_loc')
    if gi_data_loc is None or gi_data_loc == '':
        gi_data_loc = 'data/GeoIP.dat'

    logger.info('Loading GeoIP data')
    global gi_org
    gi_org = load_gi_org(gi_org_loc)
    geo_data = pygeoip.GeoIP('data/GeoIP.dat', pygeoip.MEMORY_CACHE)

    wheat = []
    enriched = []

    logger.info('Beginning winnowing process')
    for each in crop:
        indicator = each['indicator']
        indicator_type = each['indicator_type']
        if indicator_type == 'IPv4' and is_ipv4(indicator):
            ipaddr = IPAddress(indicator)
            if not reserved(ipaddr):
                wheat.append(each)
                if enrich_ip:
                    enriched.append(dict(each.items() + enrich_IPv4(ipaddr, geo_data, dnsdb).items()))
                else:
                    enriched.append(dict(each.items() + enrich_IPv4(ipaddr, geo_data).items()))
            else:
                logger.error('Found invalid address: %s from: %s' % (indicator, each['source']))
        elif (indicator_type == 'IPv4' or indicator_type == 'IPv6') and is_ipv6(indicator):  # generic cleanup
            each['indicator_type'] = 'IPv6'
            wheat.append(each)
        elif indicator_type == 'FQDN' and uniaccept.verifytldoffline(indicator, "./tld-list.txt"):
            wheat.append(each)
            # TODO: this needs logic from v0.1.2 brought forward
            if enrich_dns:
                enriched.append(dict(each.items() + enrich_FQDN(indicator, each['date'], dnsdb).items()))
        elif indicator_type == 'HASH':
            wheat.append(each)
            if enrich_hash:
                enriched.append(dict(each.items() + enrich_hash(indicator)))
        elif indicator_type == 'URL':
            wheat.append(each)
        else:
            logger.error('Could not determine address type for %s listed as %s' % (indicator, indicator_type))

    logger.info('Dumping results')
    with open(out_file, 'wb') as f:
        w_data = json.dumps(wheat, indent=2, ensure_ascii=False).encode('utf8')
        f.write(w_data)

    with open(enr_file, 'wb') as f:
        e_data = json.dumps(enriched, indent=2, ensure_ascii=False).encode('utf8')
        f.write(e_data)


def main():
    winnow('crop.json', 'crop.json', 'enriched.json')

if __name__ == "__main__":
    main()

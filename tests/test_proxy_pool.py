# -*- coding: utf-8 -*-
"""ProxyPool 过滤逻辑单元测试(不依赖网络)。

运行: pytest tests/test_proxy_pool.py
"""
from weibo.utils.proxy_pool import ProxyPool


def test_filter_http_keeps_prefixed():
    raw = ['http://1.2.3.4:8080', 'https://5.6.7.8:443',
           'socks5://9.9.9.9:1080', 'http://1.2.3.4:8080', '']
    assert ProxyPool._filter_http(raw) == ['http://1.2.3.4:8080',
                                           'https://5.6.7.8:443']


def test_filter_http_adds_prefix_to_bare_host_port():
    """付费代理 API 常返回不带协议的 ip:port / host:port,自动补 http://。"""
    raw = ['10.0.0.1:3128', 'tps.kdlapi.com:15818', '1.2.3.4']
    assert ProxyPool._filter_http(raw) == ['http://10.0.0.1:3128',
                                           'http://tps.kdlapi.com:15818']


def test_filter_http_drops_credentials_and_other_protocols():
    raw = ['user:pass@1.2.3.4:8080', 'socks5://1.2.3.4:1080',
           'ftp://1.2.3.4:21', 'weird line']
    assert ProxyPool._filter_http(raw) == []

#!/bin/sh
set -eu
# This short-lived helper alone receives NET_ADMIN. The runtime never does.
controller="$1"
proxy="${2:-}"
iptables -F OUTPUT
iptables -P OUTPUT DROP
iptables -F INPUT
iptables -P INPUT DROP
iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A INPUT -s "$controller" -p tcp --dport 8000 -j ACCEPT
# Block Docker's embedded resolver, including its DNAT high ports, before loopback.
iptables -A OUTPUT -d 127.0.0.11 -j DROP
iptables -A OUTPUT -p udp --dport 53 -j DROP
iptables -A OUTPUT -p tcp --dport 53 -j DROP
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
if [ -n "$proxy" ]; then
    iptables -A OUTPUT -d "$proxy" -p tcp --dport 3128 -j ACCEPT
fi
ip6tables -F INPUT
ip6tables -P INPUT DROP
ip6tables -A INPUT -i lo -j ACCEPT
ip6tables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ip6tables -F OUTPUT
ip6tables -P OUTPUT DROP
ip6tables -A OUTPUT -o lo -j ACCEPT
ip6tables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

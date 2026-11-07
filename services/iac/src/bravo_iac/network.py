"""The network as declared: every environment's tailnet routes in one view (T44, ADR-0032).

One answer to "how does X reach Y" for operators and agents at plan time: per environment, the
egress routes (the in-cluster Service a client calls -> tailnet device, tag, port) and the exposed
devices (tailnet host name -> in-cluster selector and port), and the tailnet policy fragment per
tailnet. It is read from the committed desired states only: nothing is observed, so no caller has to
look up proxy Pods, ports or addresses. The tailnet suffix stays a placeholder.
"""

from .catalog import check_state
from .modules import IacError, tailnet_of
from .plan import digest
from .tailnet import fragment


def _dns(namespace, service):
    return f"{service}.{namespace}.svc.cluster.local"


def view(states) -> dict:
    environments, tailnets = {}, set()
    for raw in states:
        state = check_state(raw)
        if state["environment"] in environments:
            raise IacError("IAC_ENVIRONMENT_DUPLICATE")
        egress, ingress, access = [], [], []
        for entry in state["modules"]:
            params = entry["params"]
            if entry["id"] == "tailnet-egress":
                for group in params["groups"]:
                    tailnets.add(tailnet_of(group))
                    egress += [{"service": _dns(r["namespace"], r["service"]), "port": r["port"],
                                "destination": r["destination"], "destination_tag": r["destination_tag"],
                                "tailnet": tailnet_of(group), "group": group["name"], "group_tag": group["tag"],
                                "clients": group["clients"], "client_policy": group.get("client_policy", True)}
                               for r in group["routes"]]
            elif entry["id"] == "operator-access":
                tailnets.add(tailnet_of(params))
                access += [{"proxy": params["proxy_hostname"], "proxy_tag": params["proxy_tag"],
                            "tailnet": tailnet_of(params), "tailnet_group": b["tailnet_group"],
                            "group": b["group"], "cluster_role": b["cluster_role"]} for b in params["bindings"]]
            elif entry["id"] == "tailnet-ingress":
                for device in params["devices"]:
                    tailnets.add(tailnet_of(device))
                    ingress.append({"hostname": device["hostname"], "tag": device["tag"],
                                    "tailnet": tailnet_of(device), "service": _dns(device["namespace"], device["service"]),
                                    "selector": device["selector"], "ports": device["ports"]})
        environments[state["environment"]] = {
            "digest": digest(state),
            "egress": sorted(egress, key=lambda r: (r["service"], r["port"])),
            "ingress": sorted(ingress, key=lambda d: d["hostname"]),
            "access": sorted(access, key=lambda a: (a["proxy"], a["group"])),
        }
    return {"kind": "iac-network.v1", "environments": dict(sorted(environments.items())),
            "tailnets": {key: fragment(states, key) for key in sorted(tailnets)}}

"""Small helpers to build n8n workflow JSON files."""
import json, os, uuid


def nid():
    return str(uuid.uuid4())


def cond(left, op_type, operation, right=None, single=False):
    c = {"id": nid(), "leftValue": left, "operator": {"type": op_type, "operation": operation}}
    if single:
        c["operator"]["singleValue"] = True
        c["rightValue"] = ""
    else:
        c["rightValue"] = right
    return c


def conditions(conds, combinator="and"):
    return {
        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
        "conditions": conds,
        "combinator": combinator,
    }


class Workflow:
    def __init__(self, name):
        self.name = name
        self.nodes = []
        self.links = []

    def node(self, name, ntype, version, params, pos, **extra):
        n = {"parameters": params, "id": nid(), "name": name, "type": ntype,
             "typeVersion": version, "position": pos}
        n.update(extra)
        self.nodes.append(n)
        return n

    def sticky(self, content, pos, w, h, color=None):
        p = {"content": content, "height": h, "width": w}
        if color:
            p["color"] = color
        k = len([n for n in self.nodes if n["type"].endswith("stickyNote")]) + 1
        self.nodes.append({"parameters": p, "id": nid(), "name": f"Note {k}",
                           "type": "n8n-nodes-base.stickyNote", "typeVersion": 1, "position": pos})

    def link(self, a, b, out_index=0):
        names = {n["name"] for n in self.nodes}
        assert a in names and b in names, (a, b)
        self.links.append((a, b, out_index))

    def save(self, path):
        connections = {}
        for a, b, i in self.links:
            outs = connections.setdefault(a, {"main": []})["main"]
            while len(outs) <= i:
                outs.append([])
            outs[i].append({"node": b, "type": "main", "index": 0})
        wf = {
            "name": self.name,
            "nodes": self.nodes,
            "connections": connections,
            "active": False,
            "settings": {"executionOrder": "v1"},
            "pinData": {},
            "meta": {"templateCredsSetupCompleted": False},
            "tags": [],
        }
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(wf, f, ensure_ascii=False, indent=2)
        print("wrote", path, "nodes:", len(self.nodes))


def switch_rules(rules):
    """rules: list of (output_name, condition_dict)."""
    return {
        "rules": {"values": [
            {"conditions": conditions([c]), "renameOutput": True, "outputKey": key}
            for key, c in rules
        ]},
        "options": {},
    }

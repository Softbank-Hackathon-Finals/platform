import json

import pytest

from bravo_agent.tools import PolicyDenied

RESOURCE = "arn:aws:s3:::attachments.example.invalid/*"


def allow(**changes):
    return {"Effect": "Allow", "Action": "s3:GetObject", "Resource": RESOURCE, **changes}


def deny(**changes):
    return {"Effect": "Deny", "Action": "s3:*", "Resource": RESOURCE,
            "Principal": "*", "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            **changes}


def submit(context, content):
    proposal, tools = context[2:]
    proposal["edits"][0]["replacement"] = content
    return tools.dispatch("mcp__bravo__plan_propose", proposal)


@pytest.mark.parametrize("statement", [
    deny(), deny(Principal={"AWS": "*"}), deny(Action=["s3:GetObject", "*"], Resource="*"),
    deny(Principal={"AWS": ["example-principal", "*"]}),
    allow(Principal={"Service": "lambda.amazonaws.com"}),
    allow(Condition={"StringLike": {"s3:prefix": "*"}}),
])
@pytest.mark.parametrize("as_list", [False, True])
def test_structured_policy_accepts_deny_and_scoped_allow(context, statement, as_list):
    document = {"Version": "2012-10-17", "Statement": [statement] if as_list else statement}
    assert submit(context, json.dumps(document))["status"] == "PROPOSAL_RECEIVED"
    assert context[3].events[-1]["outcome"] == "ALLOWED"
    assert (context[1].root / "src/config.py").read_text() == 'DB_HOST = "localhost"\n'


@pytest.mark.parametrize("statement", [
    allow(Action="*"), allow(Action=["s3:GetObject", "*"]),
    allow(Action=["*", "s3:GetObject"]), allow(Resource=[RESOURCE, "*"]),
    allow(Principal="*"), allow(Principal={"AWS": "*"}),
    allow(Principal={"AWS": ["example-principal", "*"]}),
    allow(Principal={"Service": "lambda.amazonaws.com", "AWS": "*"}),
    allow(Principal="*", Condition={"Bool": {"aws:SecureTransport": "true"}}),
    allow(NotAction="s3:GetObject"), allow(NotResource=RESOURCE),
    allow(NotPrincipal={"AWS": "example-principal"}),
])
def test_grant_wildcards_are_rejected_in_any_statement_position(context, statement):
    document = {"Statement": [deny(), statement]}
    with pytest.raises(PolicyDenied):
        submit(context, json.dumps(document))
    tools = context[3]
    assert tools.proposal is None
    assert tools.events[-1]["outcome"] == "DENIED"
    assert tools.events[-1]["reason"] == "INVALID_INPUT_OR_POLICY"
    assert RESOURCE not in json.dumps(tools.events)


@pytest.mark.parametrize("content", [
    '{"Statement":[{"Effect":"Deny","Effect":"Allow","Principal":"*"}]}',
    '{"Statement":[{"Effect":"Allow","Effect":"Deny","Principal":"*"}]}',
    '{"Effect":"Allow","Action":"*","Action":"s3:GetObject"}',
    '{"Effect":"Deny","Statement":[{"Action":"*"}]}',
    '{"Statement":[{"Effect":"deny","Principal":"*"}]}',
    '{"Statement":[{"Effect":null,"Principal":"*"}]}',
    '{"Statement":[{"Principal":"*"}]}',
    '{"Statement":[]}', '{"Statement":"Deny"}',
    '{"Statement":[{"Effect":"Deny","Action":["s3:GetObject",{}]}]}',
    '{"Statement":[{"Effect":"Deny","Principal":{"Unknown":"*"}}]}',
    '{"Statement":[{"Effect":"Deny","Principal":{"AWS":[]}}]}',
    '{"Statement":[{"Effect":"Deny","Principal":"*"}',
    'policy = {"Effect":"Allow","Action":["s3:GetObject","*"]}',
    "policy = {'Effect': 'Deny', 'Principal': '*'}",
    'Effect: Allow\nAction: ["s3:GetObject", "*"]',
    'Effect = "Allow"\nAction = ["s3:GetObject", "*"]',
])
def test_ambiguous_or_unsupported_policy_is_rejected_and_audited(context, content):
    with pytest.raises(PolicyDenied):
        submit(context, content)
    assert context[3].proposal is None
    assert context[3].events[-1]["outcome"] == "DENIED"


@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize("permitted", [False, True])
def test_wrapped_and_serialized_policies_keep_statement_effect(context, encoded, permitted):
    document = {"Statement": [deny() if permitted else allow(Principal={"AWS": "*"})]}
    content = json.dumps({"Properties": {"PolicyDocument": json.dumps(document) if encoded else document}})
    if permitted:
        assert submit(context, content)["status"] == "PROPOSAL_RECEIVED"
    else:
        with pytest.raises(PolicyDenied):
            submit(context, content)


def test_unicode_escaped_policy_values_are_checked_after_decoding(context):
    with pytest.raises(PolicyDenied):
        submit(context, r'{"Effect":"Allow","Act\u0069on":["s3:GetObject","\u002a"]}')


@pytest.mark.parametrize("reverse", [False, True])
def test_deny_and_scoped_allow_can_coexist_without_effect_inheritance(context, reverse):
    statements = [deny(), allow()]
    if reverse:
        statements.reverse()
    assert submit(context, json.dumps({"Statement": statements}))["status"] == "PROPOSAL_RECEIVED"


def test_malformed_json_with_escaped_policy_keys_is_rejected(context):
    with pytest.raises(PolicyDenied):
        submit(context, r'{"\u0045ffect":"Allow","Act\u0069on":"\u002a"')


@pytest.mark.parametrize("depth", [70, 2000])
def test_excessive_json_nesting_is_denied_and_audited(context, depth):
    with pytest.raises(PolicyDenied):
        submit(context, "[" * depth + "0" + "]" * depth)
    assert context[3].events[-1]["outcome"] == "DENIED"

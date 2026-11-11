from copy import deepcopy

from agent_pipeline.reconciliation.accounts import match_accounts


def account(
    oid, aid="A1", owner="Alex", platform="Platform", kind="ISA", status="open"
):
    return {
        "observation_id": oid,
        "observation_type": "account",
        "source_id": "source-1",
        "data": {
            "account": {
                "account_id": aid,
                "platform": platform,
                "account_type": kind,
                "owner_text": owner,
                "reference_text": "Source account description",
            },
            "holders": [{"name": owner, "reference_text": owner}],
            "status": status,
        },
    }


def match(*observations):
    return match_accounts({"observations": list(observations)})


def test_same_explicit_id_consolidates_joint_records_without_mutating_evidence():
    observations = [account("o1", owner="Alex"), account("o2", owner="Sam")]
    before = deepcopy(observations)
    result = match(*observations)
    assert len(result["accounts"]) == 1
    assert result["accounts"][0]["observation_ids"] == ["o1", "o2"]
    assert observations == before


def test_different_ids_stay_separate_even_with_identical_descriptions():
    assert len(match(account("o1", "A1"), account("o2", "A2"))["accounts"]) == 2
    assert len(match(account("o1", "a1"), account("o2", "A1"))["accounts"]) == 2


def test_missing_id_matches_only_a_unique_owner_platform_type_regardless_of_order():
    identified = account("o1")
    unidentified = account(
        "o2", None, owner=" ALEX  ", platform=" platform ", kind="isa"
    )
    for observations in ([identified, unidentified], [unidentified, identified]):
        result = match(*observations)
        assert len(result["accounts"]) == 1
        assert result["issues"] == []
        assert (
            result["accounts"][0]["references"][-1]["match_basis"]
            == "unique_owner_platform_type"
        )


def test_multiple_possible_accounts_remain_unresolved():
    result = match(account("o1", "A1"), account("o2", None), account("o3", "A2"))
    assert result["issues"][0]["reason"] == "multiple_matches"
    assert len(result["issues"][0]["candidate_account_keys"]) == 2
    assert all("o2" not in a["observation_ids"] for a in result["accounts"])


def test_no_matching_id_anchor_does_not_invent_an_account_identity():
    result = match(account("o1", None))
    assert result["accounts"] == []
    assert result["issues"][0]["reason"] == "no_match"


def test_missing_owner_or_platform_prevents_fallback():
    for unlinked in (
        account("o2", None, owner=None),
        account("o2", None, platform=None),
    ):
        result = match(account("o1"), unlinked)
        assert result["issues"][0]["reason"] == "insufficient_identity"


def test_scope_reference_matches_unique_account_without_assuming_ownership():
    scope = {
        "observation_id": "scope",
        "observation_type": "report_scope",
        "data": {
            "clients": [{"name": "Sam"}],
            "included_accounts": [
                {**account("unused", None)["data"]["account"], "owner_text": None}
            ],
        },
    }
    result = match(account("o1"), scope)
    assert result["issues"] == []
    assert (
        result["accounts"][0]["references"][-1]["match_basis"] == "unique_platform_type"
    )
    assert result["accounts"][0]["observation_ids"] == ["o1", "scope"]


def test_proposed_account_with_existing_id_is_separate_and_flagged():
    result = match(account("o1"), account("o2", status="proposed"))
    assert len(result["accounts"]) == 2
    assert result["issues"][0]["issue_type"] == "proposed_existing_id_conflict"


def test_proposed_account_without_id_cannot_match_existing_account():
    result = match(account("o1"), account("o2", None, status="proposed"))
    assert result["issues"][0]["reason"] == "no_match"
    assert result["accounts"][0]["observation_ids"] == ["o1"]


def test_action_and_valuation_references_link_by_explicit_id():
    reference = account("unused")["data"]["account"]
    valuation = {
        "observation_id": "value",
        "observation_type": "valuation",
        "data": {"account": reference},
    }
    action = {
        "observation_id": "action",
        "observation_type": "action",
        "data": {
            "action_type": "sell",
            "source_accounts": [reference],
            "target_accounts": [],
        },
    }
    result = match(account("o1"), valuation, action)
    assert result["accounts"][0]["observation_ids"] == ["o1", "value", "action"]
    assert result["issues"] == []


def test_open_account_targets_do_not_merge_with_existing_holdings():
    action = {
        "observation_id": "action",
        "observation_type": "action",
        "data": {
            "action_type": "open_account",
            "target_accounts": [account("unused")["data"]["account"]],
        },
    }
    result = match(account("o1"), action)
    assert len(result["accounts"]) == 2
    assert result["accounts"][1]["proposed"] is True


def test_empty_input_and_absent_optional_account_produce_no_matches():
    assert match() == {"accounts": [], "issues": []}
    assert match(
        {
            "observation_id": "funding",
            "observation_type": "funding",
            "data": {"account": None},
        }
    ) == {"accounts": [], "issues": []}


def test_explicit_identity_uncertainty_blocks_description_fallback():
    uncertain = account("o2", None)
    uncertain["field_states"] = [{"field": "account.account_id", "state": "ambiguous"}]
    result = match(account("o1"), uncertain)
    assert result["issues"][0]["reason"] == "ambiguous_reference"
    assert result["accounts"][0]["observation_ids"] == ["o1"]


def test_idless_action_links_source_and_target_by_unique_stated_description():
    cash = account("cash", "C1", kind="Cash Account")
    isa = account("isa", "I1", kind="ISA")
    source = {**cash["data"]["account"], "account_id": None}
    target = {**isa["data"]["account"], "account_id": None}
    action = {
        "observation_id": "action",
        "observation_type": "action",
        "data": {
            "action_type": "contribute",
            "source_accounts": [source],
            "target_accounts": [target],
        },
    }
    result = match(cash, isa, action)
    assert result["issues"] == []
    assert all(a["observation_ids"][-1] == "action" for a in result["accounts"])
    assert source["account_id"] is None and target["account_id"] is None


def test_description_match_stays_unresolved_when_two_accounts_fit():
    reference = {
        **account("unused")["data"]["account"],
        "account_id": None,
        "owner_text": None,
    }
    scope = {
        "observation_id": "scope",
        "observation_type": "report_scope",
        "data": {"included_accounts": [reference]},
    }
    result = match(
        account("o1", "A1", owner="Alex"), account("o2", "A2", owner="Sam"), scope
    )
    assert result["issues"][0]["reason"] == "multiple_matches"
    assert len(result["issues"][0]["candidate_account_keys"]) == 2


def test_linked_ambiguous_reference_prevents_description_matching():
    reference = {
        **account("unused")["data"]["account"],
        "account_id": None,
        "owner_text": None,
    }
    scope = {
        "observation_id": "scope",
        "observation_type": "report_scope",
        "data": {"included_accounts": [reference]},
    }
    extraction = {
        "observations": [account("o1"), scope],
        "extraction_issues": [
            {
                "issue_type": "ambiguous_reference",
                "observation_ids": ["scope"],
                "affected_fields": [],
            }
        ],
    }
    result = match_accounts(extraction)
    assert result["issues"][0]["reason"] == "ambiguous_reference"


def described_scope(owner, kind="ISA", platform="Platform"):
    return {
        "observation_id": "scope",
        "observation_type": "report_scope",
        "data": {
            "included_accounts": [
                {
                    "account_id": None,
                    "platform": platform,
                    "account_type": kind,
                    "owner_text": owner,
                    "reference_text": "Original stated description",
                }
            ]
        },
        "field_states": [
            {"field": "included_accounts[0].account_id", "state": "not_stated"}
        ],
    }


def test_stated_owner_disambiguates_two_isas_without_importing_ids():
    scope = described_scope("Sam")
    result = match(
        account("alex", "A1", owner="Alex Taylor", kind="Stocks & Shares ISA"),
        account("sam", "A2", owner="Sam Jordan", kind="Stocks & Shares ISA"),
        scope,
    )
    assert result["issues"] == []
    assert "scope" not in result["accounts"][0]["observation_ids"]
    assert "scope" in result["accounts"][1]["observation_ids"]
    assert scope["data"]["included_accounts"][0]["account_id"] is None


def test_shared_first_name_is_not_enough_to_select_an_owner():
    result = match(
        account("a", "A1", owner="Alex Taylor"),
        account("b", "A2", owner="Alex Jordan"),
        described_scope("Alex"),
    )
    assert result["issues"][0]["reason"] == "multiple_matches"
    result = match(
        account("a", "A1", owner="Alex Taylor"),
        account("b", "A2", owner="Alex Jordan"),
        described_scope("Alex Jordan"),
    )
    assert result["issues"] == []
    assert "scope" in result["accounts"][1]["observation_ids"]


def test_supplied_owner_cannot_be_ignored_for_a_unique_platform_type():
    result = match(account("a", "A1", owner="Alex Taylor"), described_scope("Sam"))
    assert result["issues"][0]["reason"] == "no_match"


def test_generic_isa_remains_ambiguous_between_cash_and_investment_isas():
    result = match(
        account("a", "A1", kind="Stocks & Shares ISA"),
        account("b", "A2", kind="Cash ISA"),
        described_scope("Alex"),
    )
    assert result["issues"][0]["reason"] == "multiple_matches"


def test_joint_owner_and_gia_abbreviation_link_duplicated_joint_records():
    result = match(
        account("a", "J1", owner="Alex Taylor", kind="General Investment Account"),
        account("b", "J1", owner="Sam Jordan", kind="General Investment Account"),
        described_scope("joint", "GIA"),
    )
    assert result["issues"] == []
    assert len(result["accounts"]) == 1
    assert "scope" in result["accounts"][0]["observation_ids"]


def test_missing_platform_and_unclear_cash_reference_stay_unresolved():
    result = match(
        account("cash", "C1", kind="Cash Account"),
        described_scope(None, "Cash Account", None),
    )
    assert result["issues"][0]["reason"] == "insufficient_identity"


def test_proposed_joint_account_is_not_matched_to_existing_joint_gia():
    proposed = {
        "observation_id": "new",
        "observation_type": "action",
        "data": {
            "action_type": "open_account",
            "target_accounts": [
                {
                    "account_id": None,
                    "platform": "Platform",
                    "account_type": "GIA",
                    "owner_text": "joint",
                    "reference_text": "new joint account",
                }
            ],
        },
    }
    result = match(
        account("a", "J1", owner="Alex Taylor", kind="General Investment Account"),
        account("b", "J1", owner="Sam Jordan", kind="General Investment Account"),
        proposed,
    )
    assert result["issues"][0]["reason"] == "no_match"
    assert "new" not in result["accounts"][0]["observation_ids"]


def grouped_scope(names=("Alex", "Sam"), text="Platform ISAs (Alex and Sam)"):
    return {
        "observation_id": "scope",
        "observation_type": "report_scope",
        "data": {
            "clients": [{"name": name} for name in names],
            "included_accounts": [
                {
                    "account_id": None,
                    "platform": "Platform",
                    "account_type": "ISA",
                    "owner_text": None,
                    "reference_text": text,
                }
            ],
        },
    }


def test_explicit_named_plural_scope_links_each_unique_account_without_mutation():
    scope = grouped_scope()
    before = deepcopy(scope)
    result = match(
        account("alex", "A", owner="Alex"),
        account("sam", "S", owner="Sam"),
        account("third", "T", owner="Taylor"),
        scope,
    )
    assert result["issues"] == []
    assert {
        a["account_id"] for a in result["accounts"] if "scope" in a["observation_ids"]
    } == {"A", "S"}
    assert scope == before


def test_plural_scope_does_not_guess_when_one_person_has_two_possible_accounts():
    result = match(
        account("alex1", "A1", owner="Alex"),
        account("alex2", "A2", owner="Alex"),
        account("sam", "S", owner="Sam"),
        grouped_scope(),
    )
    assert result["issues"]
    assert all("scope" not in a["observation_ids"] for a in result["accounts"])


def test_named_group_is_not_reduced_to_the_one_account_that_matched():
    result = match(account("alex", "A", owner="Alex"), grouped_scope())
    assert result["issues"]
    assert "scope" not in result["accounts"][0]["observation_ids"]


def test_generic_plural_scope_without_names_is_not_permission_to_include_everyone():
    result = match(
        account("alex", "A", owner="Alex"),
        account("sam", "S", owner="Sam"),
        grouped_scope(text="Platform ISAs"),
    )
    assert result["issues"]
    assert all("scope" not in a["observation_ids"] for a in result["accounts"])

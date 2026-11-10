"""Conservative account matching; observations and financial facts stay unchanged."""


def _normalise(value):
    return " ".join(value.split()).casefold() if value else ""


def _type_matches(stated, known):
    stated, known = _normalise(stated), _normalise(known)
    aliases = {"gia": "general investment account"}
    stated, known = aliases.get(stated, stated), aliases.get(known, known)
    # A generic ISA reference can match a subtype only when the full candidate
    # search yields one account. Never assume that an ISA is Stocks & Shares.
    return stated == known or (stated == "isa" and known.endswith(" isa"))


def _owner_matches(stated, owners):
    stated = _normalise(stated)
    if stated in {"joint", "jointly held", "jointly-held"}:
        return len(owners) > 1 or stated in owners
    names = stated.replace(" & ", " and ").split(" and ")
    return all(
        any(name == owner or name == owner.split()[0] for owner in owners)
        for name in names
    )


def _identity(observation, reference):
    # Only account observations state account holders explicitly. Scope clients or
    # funding recipients are not sufficient evidence of account ownership.
    if any(
        state["state"] in {"ambiguous", "unreadable"}
        and (
            state["field"] == "account"
            or state["field"].startswith(("account.", "holders"))
        )
        for state in observation.get("field_states", [])
    ):
        return None
    holders = observation["data"].get("holders", [])
    owners = tuple(
        sorted({_normalise(person["name"]) for person in holders if person["name"]})
    )
    platform, account_type = _normalise(reference["platform"]), _normalise(
        reference["account_type"]
    )
    if holders and len(owners) == len(holders) and platform and account_type:
        return owners, platform, account_type
    return None


def _references(observation):
    data = observation["data"]
    if isinstance(data.get("account"), dict):
        yield "account", data["account"]
    for field in (
        "included_accounts",
        "excluded_accounts",
        "source_accounts",
        "target_accounts",
        "accounts",
    ):
        for index, reference in enumerate(data.get(field, [])):
            yield f"{field}[{index}]", reference


def match_accounts(extraction: dict) -> dict:
    accounts, issues, entries = [], [], []
    by_id, identities = {}, {}
    for observation in extraction["observations"]:
        for field, reference in _references(observation):
            proposed = (
                observation["observation_type"] == "account"
                and observation["data"]["status"] == "proposed"
            ) or (
                observation["observation_type"] == "action"
                and observation["data"]["action_type"] == "open_account"
                and field.startswith("target_accounts[")
            )
            entries.append((observation, field, reference, proposed))

    def link(account, observation, field, basis):
        oid = observation["observation_id"]
        if oid not in account["observation_ids"]:
            account["observation_ids"].append(oid)
        account["references"].append(
            {"observation_id": oid, "field": field, "match_basis": basis}
        )

    # Seed all explicit IDs before attempting fallbacks, so input order cannot
    # hide a second possible account. IDs are exact; never normalise their case.
    ordered = sorted(
        entries,
        key=lambda entry: (entry[0]["observation_type"] != "account", not entry[3]),
    )
    for observation, field, reference, proposed in ordered:
        aid = reference["account_id"]
        if not aid:
            continue
        if (
            observation["observation_type"] != "account"
            and not proposed
            and (aid, True) in by_id
            and (aid, False) not in by_id
        ):
            proposed = True
        key = (aid, proposed)
        if key not in by_id:
            account = {
                "account_key": f"account-{len(accounts) + 1:04d}",
                "account_id": aid,
                "proposed": proposed,
                "observation_ids": [],
                "references": [],
            }
            accounts.append(account)
            by_id[key] = account
            identities[key] = set()
        link(by_id[key], observation, field, "explicit_id")
        identity = _identity(observation, reference)
        if identity:
            identities[key].add(identity)

    for observation, field, reference, proposed in entries:
        if reference["account_id"]:
            continue
        uncertain = any(
            state["state"] in {"ambiguous", "unreadable"}
            and (state["field"] == field or state["field"].startswith(field + "."))
            for state in observation.get("field_states", [])
        ) or any(
            issue["issue_type"] == "ambiguous_reference"
            and observation["observation_id"] in issue["observation_ids"]
            and (
                not issue["affected_fields"]
                or any(
                    name == field or name.startswith(field + ".")
                    for name in issue["affected_fields"]
                )
            )
            for issue in extraction.get("extraction_issues", [])
        )
        identity = _identity(observation, reference)
        pair = (
            _normalise(reference["platform"]),
            _normalise(reference["account_type"]),
        )
        # References in scope/actions have no holder field. Match their stated
        # platform/type only when exactly one identified account fits this case.
        description_only = observation["observation_type"] != "account" and all(pair)
        candidates = []
        if not uncertain:
            for key, account in by_id.items():
                if key[1] != proposed:
                    continue
                known = identities[key]
                if identity is not None:
                    if any(
                        identity[0] == item[0]
                        and identity[1] == item[1]
                        and _type_matches(identity[2], item[2])
                        for item in known
                    ):
                        candidates.append(account)
                elif description_only:
                    matching = [
                        item
                        for item in known
                        if item[1] == pair[0] and _type_matches(pair[1], item[2])
                    ]
                    owners = {owner for item in matching for owner in item[0]}
                    stated_owner = reference.get("owner_text")
                    if matching and (
                        not stated_owner or _owner_matches(stated_owner, owners)
                    ):
                        candidates.append(account)
        if len(candidates) == 1:
            basis = (
                "unique_owner_platform_type"
                if identity is not None or reference.get("owner_text")
                else "unique_platform_type"
            )
            link(candidates[0], observation, field, basis)
        else:
            issues.append(
                {
                    "issue_type": "unresolved_account_match",
                    "observation_id": observation["observation_id"],
                    "field": field,
                    "reason": "ambiguous_reference"
                    if uncertain
                    else (
                        "multiple_matches"
                        if candidates
                        else "no_match"
                        if identity is not None or description_only
                        else "insufficient_identity"
                    ),
                    "candidate_account_keys": [
                        account["account_key"] for account in candidates
                    ],
                }
            )

    # A proposed account reusing an existing ID is a conflict, not permission to
    # treat it as the existing holding.
    for aid, proposed in by_id:
        if proposed and (aid, False) in by_id:
            issues.append(
                {
                    "issue_type": "proposed_existing_id_conflict",
                    "account_id": aid,
                    "account_keys": [
                        by_id[(aid, False)]["account_key"],
                        by_id[(aid, True)]["account_key"],
                    ],
                }
            )
    return {"accounts": accounts, "issues": issues}

def candidate_inventory():
    return [
        {'route':'sub2api','status':'implemented','gap':'requires exclusive local group and matching usage evidence'},
        {'route':'codex_http','status':'implemented','gap':'custom HTTP client; not official client baseline'},
        {'route':'official_cli','status':'acceptance_passed','gap':'one paired task correct on both native and HTTP; upstream model identity and quality advantage unverified; no customer gateway integration'},
        {'route':'codex_ws','status':'sub2_native_acceptance_passed','gap':'requires WS client ingress; quality advantage unverified'},
        {'route':'bps','status':'full_contract_http_403','gap':'two full-protocol requests blocked for this account; access allowed does not establish inference eligibility'},
        {'route':'siwc','status':'login_implemented','gap':'official consent, direct scope, account-specific catalog and inference acceptance pending; commercial hosted scope separate'},
    ]

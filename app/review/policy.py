def approval_allowed(payload):
    if int(payload["summary"].get("REJECT", 0)) != 0:
        return False
    board = payload.get("review_board") or {}
    # An unattributable bad row suspends the whole batch until rectified.
    if board.get("batch_hold"):
        return False
    return True

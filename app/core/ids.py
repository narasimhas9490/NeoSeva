from ulid import ULID


def new_id(prefix):
    """Create a primary key as a type prefix plus a ULID.
    ULIDs sort by creation time so indexes stay in insert order.
    Example: new_id("bkg") gives bkg_01J8F3K2M9P4QR7TVXYZ0ABCDE."""
    return f"{prefix}_{ULID()}"

// Card Placements: where each printed copy of a deck card sits in the
// project's custom order (the Decklist's "Custom" sort). See docs/adr/0009.
//
// One row per printed copy: a card at quantity 4 owns four placements, and
// those four can sit anywhere in the order, between other cards' copies.
// `project_cards.quantity` stays the source of truth for HOW MANY copies a
// card has — the Row view's stepper, every import path and the server's
// entries all keep reading it — and this table only says in what order those
// copies print. `reconcile` keeps the two in step instead of every insert,
// remove and quantity path having to remember this table exists:
//
//   · a placement whose card is gone is dropped (foreign keys aren't enforced
//     on this database, so nothing cascades);
//   · a card with more placements than copies loses its last ones;
//   · a card with fewer gets the new copies right after its last placement —
//     raising a 2-of to a 3-of puts the third beside the other two — or, for a
//     card with none yet (just added), at the end;
//   · positions are then renumbered 0..n-1, densely: the order has no holes,
//     a drop inserts and the rest shift.
//
// A project that has never had a placement seeds itself the same way, in
// sort_order with each card's copies together, so existing projects heal on
// their first read.
//
// A double-faced card's transform side has placements of its own (face 1),
// one per copy, for when it prints as a card of its own rather than on its
// front's back (docs/adr/0009). They are kept whatever the PDF tab says, so a
// transform dragged somewhere keeps that spot while pairing is on and gets it
// back when pairing goes off; the Grid only draws them while they print on
// their own, and the server ignores them while they don't. Rust can't tell a
// double-faced card from any other, so the client names the ones it knows
// (`get_card_placements`), and a card that already has transform rows is
// double-faced from then on:
//
//   · a card's first transforms go right after its fronts, the k-th after the
//     k-th — exactly where the PDF printed them before they had placements;
//   · later ones (a quantity raised) come in front/transform pairs after the
//     card's last placement, and a new card's go at the end the same way;
//   · they're trimmed to the card's quantity from the end, like fronts;
//   · a card missing from the client's list keeps its transform rows: that
//     list grows as faces load, and an early, short one mustn't wipe them.
use std::collections::{HashMap, HashSet};

use rusqlite::{params, Connection, OptionalExtension};
use serde::Serialize;
use tauri::AppHandle;

use crate::project_store::open_db;

/// The card itself.
pub(crate) const FACE_FRONT: i64 = 0;
/// A double-faced card's transform side, placed as a card of its own.
pub(crate) const FACE_TRANSFORM: i64 = 1;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CardPlacement {
    pub id: i64,
    pub card_id: i64,
    pub position: i64,
    /// FACE_FRONT or FACE_TRANSFORM.
    pub face: i64,
}

/// One row of the order being built: (existing placement id, card, face).
type Row = (Option<i64>, i64, i64);

/// Bring the project's placements in line with its cards' quantities, and
/// return them in order. `double_faced` names cards the client knows to be
/// double-faced, which get transform rows if they have none yet. Writes only
/// when something actually changed, so the common read is a read.
pub(crate) fn reconcile(
    conn: &Connection,
    project_id: i64,
    double_faced: &[i64],
) -> Result<Vec<CardPlacement>, String> {
    let cards: Vec<(i64, i64)> = {
        let mut stmt = conn
            .prepare(
                "SELECT id, COALESCE(quantity, 1) FROM project_cards
                 WHERE project_id = ?1 ORDER BY sort_order ASC, id ASC",
            )
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map(params![project_id], |r| Ok((r.get(0)?, r.get::<_, i64>(1)?.max(1))))
            .map_err(|e| e.to_string())?;
        rows.collect::<Result<_, _>>().map_err(|e| e.to_string())?
    };
    let existing: Vec<CardPlacement> = read_placements(conn, project_id)?;

    let wanted: HashMap<i64, i64> = cards.iter().copied().collect();
    let mut to_delete: Vec<i64> = Vec::new();

    // Live placements in order, orphans dropped, then each card's fronts and
    // transforms trimmed to its quantity from the end.
    let mut kept: Vec<Row> = Vec::with_capacity(existing.len());
    for p in &existing {
        if wanted.contains_key(&p.card_id) {
            kept.push((Some(p.id), p.card_id, p.face));
        } else {
            to_delete.push(p.id);
        }
    }
    let mut seen: HashMap<(i64, i64), i64> = HashMap::new();
    for &(_, card_id, face) in &kept {
        *seen.entry((card_id, face)).or_default() += 1;
    }
    let mut surplus: HashMap<(i64, i64), i64> = seen
        .iter()
        .filter_map(|(&key, &have)| {
            let want = wanted[&key.0];
            (have > want).then_some((key, have - want))
        })
        .collect();
    if !surplus.is_empty() {
        let mut trimmed: Vec<Row> = Vec::with_capacity(kept.len());
        for &(id, card_id, face) in kept.iter().rev() {
            match surplus.get_mut(&(card_id, face)) {
                Some(n) if *n > 0 => {
                    *n -= 1;
                    if let Some(id) = id {
                        to_delete.push(id);
                    }
                }
                _ => trimmed.push((id, card_id, face)),
            }
        }
        trimmed.reverse();
        kept = trimmed;
    }

    let mut fronts: HashMap<i64, i64> = HashMap::new();
    let mut transforms: HashMap<i64, i64> = HashMap::new();
    for &(_, card_id, face) in &kept {
        if face == FACE_TRANSFORM {
            *transforms.entry(card_id).or_default() += 1;
        } else {
            *fronts.entry(card_id).or_default() += 1;
        }
    }
    let named: HashSet<i64> = double_faced.iter().copied().collect();
    let is_dfc = |card_id: i64| named.contains(&card_id) || transforms.contains_key(&card_id);
    // Missing copies of a card, as front/transform pairs: what goes after its
    // last placement, or at the end for a card with none.
    let pairs = |card_id: i64, have_fronts: i64, have_transforms: i64| -> Vec<Row> {
        let want = wanted[&card_id];
        let missing_fronts = want - have_fronts;
        let missing_transforms = if is_dfc(card_id) { want - have_transforms } else { 0 };
        let mut out = Vec::new();
        for j in 0..missing_fronts.max(missing_transforms) {
            if j < missing_fronts {
                out.push((None, card_id, FACE_FRONT));
            }
            if j < missing_transforms {
                out.push((None, card_id, FACE_TRANSFORM));
            }
        }
        out
    };

    let mut last_index: HashMap<i64, usize> = HashMap::new();
    for (i, &(_, card_id, _)) in kept.iter().enumerate() {
        last_index.insert(card_id, i);
    }
    let mut order: Vec<Row> = Vec::with_capacity(kept.len());
    // A double-faced card with no transform rows yet takes its first ones
    // beside its fronts, the k-th after the k-th.
    let mut seeding: HashMap<i64, i64> = HashMap::new();
    for (&card_id, &n) in &fronts {
        if is_dfc(card_id) && !transforms.contains_key(&card_id) {
            seeding.insert(card_id, n.min(wanted[&card_id]));
        }
    }
    let mut fronts_seen: HashMap<i64, i64> = HashMap::new();
    let mut seeded: HashMap<i64, i64> = HashMap::new();
    for (i, &entry) in kept.iter().enumerate() {
        order.push(entry);
        let (_, card_id, face) = entry;
        if face == FACE_FRONT {
            let k = fronts_seen.entry(card_id).or_default();
            *k += 1;
            if seeding.get(&card_id).is_some_and(|&n| *k <= n) {
                order.push((None, card_id, FACE_TRANSFORM));
                *seeded.entry(card_id).or_default() += 1;
            }
        }
        if last_index.get(&card_id) == Some(&i) {
            let have_transforms = transforms.get(&card_id).copied().unwrap_or(0)
                + seeded.get(&card_id).copied().unwrap_or(0);
            order.extend(pairs(card_id, fronts.get(&card_id).copied().unwrap_or(0), have_transforms));
        }
    }
    for &(card_id, _) in &cards {
        if !last_index.contains_key(&card_id) {
            order.extend(pairs(card_id, 0, 0));
        }
    }

    let unchanged = to_delete.is_empty()
        && order.len() == existing.len()
        && order
            .iter()
            .zip(&existing)
            .enumerate()
            .all(|(i, (o, p))| o.0 == Some(p.id) && p.position == i as i64);
    if unchanged {
        return Ok(existing);
    }

    let tx = conn.unchecked_transaction().map_err(|e| e.to_string())?;
    for id in &to_delete {
        tx.execute("DELETE FROM card_placements WHERE id = ?1", params![id])
            .map_err(|e| e.to_string())?;
    }
    for (position, (id, card_id, face)) in order.iter().enumerate() {
        match id {
            Some(id) => tx.execute(
                "UPDATE card_placements SET position = ?1 WHERE id = ?2",
                params![position as i64, id],
            ),
            None => tx.execute(
                "INSERT INTO card_placements (project_id, card_id, position, face)
                 VALUES (?1, ?2, ?3, ?4)",
                params![project_id, card_id, position as i64, face],
            ),
        }
        .map_err(|e| e.to_string())?;
    }
    tx.commit().map_err(|e| e.to_string())?;
    read_placements(conn, project_id)
}

fn read_placements(conn: &Connection, project_id: i64) -> Result<Vec<CardPlacement>, String> {
    let mut stmt = conn
        .prepare(
            "SELECT id, card_id, position, face FROM card_placements
             WHERE project_id = ?1 ORDER BY position ASC, id ASC",
        )
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map(params![project_id], |r| {
            Ok(CardPlacement {
                id: r.get(0)?,
                card_id: r.get(1)?,
                position: r.get(2)?,
                face: r.get(3)?,
            })
        })
        .map_err(|e| e.to_string())?;
    rows.collect::<Result<_, _>>().map_err(|e| e.to_string())
}

/// Rewrite the order. `placement_ids` must hold every one of the project's
/// current front placements, each once, and nothing that isn't a current
/// placement: a client holding a stale list (a copy added or removed
/// elsewhere since it last read) is refused rather than half-applied, and
/// refetches.
///
/// Transform placements may be left out — the Grid doesn't draw them while
/// they print on their fronts' backs, or for a card it doesn't yet know is
/// double-faced. One left out goes back beside its front: the card's k-th
/// transform right after its k-th front, as they were first placed.
pub(crate) fn set_order(conn: &Connection, project_id: i64, placement_ids: &[i64]) -> Result<(), String> {
    let stale = || "The deck changed since this order was read — reload and try again".to_string();
    let current = reconcile(conn, project_id, &[])?;
    let by_id: HashMap<i64, &CardPlacement> = current.iter().map(|p| (p.id, p)).collect();
    let given: HashSet<i64> = placement_ids.iter().copied().collect();
    if given.len() != placement_ids.len() || !given.iter().all(|id| by_id.contains_key(id)) {
        return Err(stale());
    }
    if current.iter().any(|p| p.face == FACE_FRONT && !given.contains(&p.id)) {
        return Err(stale());
    }

    // Left-out transforms, per card, by their rank among that card's
    // transforms in the current order.
    let mut rank: HashMap<i64, usize> = HashMap::new();
    let mut omitted: HashMap<(i64, usize), Vec<i64>> = HashMap::new();
    for p in &current {
        if p.face != FACE_TRANSFORM {
            continue;
        }
        let r = rank.entry(p.card_id).or_default();
        if !given.contains(&p.id) {
            omitted.entry((p.card_id, *r)).or_default().push(p.id);
        }
        *r += 1;
    }
    let mut ids: Vec<i64> = Vec::with_capacity(current.len());
    if omitted.is_empty() {
        ids.extend_from_slice(placement_ids);
    } else {
        let mut last_index: HashMap<i64, usize> = HashMap::new();
        for (i, id) in placement_ids.iter().enumerate() {
            last_index.insert(by_id[id].card_id, i);
        }
        let mut fronts_seen: HashMap<i64, usize> = HashMap::new();
        for (i, id) in placement_ids.iter().enumerate() {
            ids.push(*id);
            let p = by_id[id];
            if p.face == FACE_FRONT {
                let k = fronts_seen.entry(p.card_id).or_default();
                if let Some(back) = omitted.remove(&(p.card_id, *k)) {
                    ids.extend(back);
                }
                *k += 1;
            }
            if last_index.get(&p.card_id) == Some(&i) {
                // Ranks past the card's last front (more transforms than
                // fronts can't survive reconcile, but stay safe): after it.
                let mut rest: Vec<((i64, usize), Vec<i64>)> = omitted
                    .iter()
                    .filter(|((card, _), _)| *card == p.card_id)
                    .map(|(k, v)| (*k, v.clone()))
                    .collect();
                rest.sort();
                for (key, back) in rest {
                    omitted.remove(&key);
                    ids.extend(back);
                }
            }
        }
        // A card with no placement in the list at all can't happen (every
        // front is required), but nothing may be dropped.
        let mut rest: Vec<((i64, usize), Vec<i64>)> = omitted.into_iter().collect();
        rest.sort();
        for (_, back) in rest {
            ids.extend(back);
        }
    }

    let tx = conn.unchecked_transaction().map_err(|e| e.to_string())?;
    for (position, id) in ids.iter().enumerate() {
        tx.execute(
            "UPDATE card_placements SET position = ?1 WHERE id = ?2",
            params![position as i64, id],
        )
        .map_err(|e| e.to_string())?;
    }
    tx.commit().map_err(|e| e.to_string())
}

/// Remove ONE printed copy — the one the user pointed at, not whichever copy
/// reconcile would trim — and the card with it when that was its last.
/// Returns the project's placements afterwards.
pub(crate) fn remove_copy(conn: &Connection, placement_id: i64) -> Result<Vec<CardPlacement>, String> {
    let found: Option<(i64, i64)> = conn
        .query_row(
            "SELECT project_id, card_id FROM card_placements WHERE id = ?1",
            params![placement_id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .map_err(|e| e.to_string())?;
    let Some((project_id, card_id)) = found else {
        return Err("That copy is no longer in the deck".into());
    };
    let quantity: Option<i64> = conn
        .query_row(
            "SELECT COALESCE(quantity, 1) FROM project_cards WHERE id = ?1",
            params![card_id],
            |r| r.get(0),
        )
        .optional()
        .map_err(|e| e.to_string())?;
    let tx = conn.unchecked_transaction().map_err(|e| e.to_string())?;
    tx.execute("DELETE FROM card_placements WHERE id = ?1", params![placement_id])
        .map_err(|e| e.to_string())?;
    match quantity {
        Some(q) if q > 1 => {
            tx.execute(
                "UPDATE project_cards SET quantity = ?1 WHERE id = ?2",
                params![q - 1, card_id],
            )
            .map_err(|e| e.to_string())?;
        }
        _ => {
            tx.execute("DELETE FROM project_cards WHERE id = ?1", params![card_id])
                .map_err(|e| e.to_string())?;
            delete_for_card(&tx, card_id)?;
        }
    }
    tx.commit().map_err(|e| e.to_string())?;
    reconcile(conn, project_id, &[])
}

pub(crate) fn delete_for_card(conn: &Connection, card_id: i64) -> Result<(), String> {
    conn.execute("DELETE FROM card_placements WHERE card_id = ?1", params![card_id])
        .map_err(|e| e.to_string())?;
    Ok(())
}

pub(crate) fn delete_for_project(conn: &Connection, project_id: i64) -> Result<(), String> {
    conn.execute("DELETE FROM card_placements WHERE project_id = ?1", params![project_id])
        .map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
pub fn get_card_placements(
    app: AppHandle,
    project_id: i64,
    double_faced: Option<Vec<i64>>,
) -> Result<Vec<CardPlacement>, String> {
    let conn = open_db(&app)?;
    reconcile(&conn, project_id, double_faced.as_deref().unwrap_or(&[]))
}

#[tauri::command]
pub fn set_placement_order(app: AppHandle, project_id: i64, placement_ids: Vec<i64>) -> Result<(), String> {
    let conn = open_db(&app)?;
    set_order(&conn, project_id, &placement_ids)
}

#[tauri::command]
pub fn remove_card_copy(app: AppHandle, placement_id: i64) -> Result<Vec<CardPlacement>, String> {
    let conn = open_db(&app)?;
    remove_copy(&conn, placement_id)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::project_store::test_support::test_conn;

    // The one project these tests use: the first an empty database creates.
    const P: i64 = 1;

    fn conn() -> Connection {
        let conn = test_conn();
        let id = crate::project_store::get_or_create_unnamed_project_id(&conn).unwrap();
        assert_eq!(id, P);
        conn
    }

    fn add_card(conn: &Connection, name: &str, quantity: i64) -> i64 {
        let next: i64 = conn
            .query_row(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM project_cards WHERE project_id = ?1",
                params![P],
                |r| r.get(0),
            )
            .unwrap();
        conn.execute(
            "INSERT INTO project_cards (project_id, sort_order, original_import_line, quantity, name)
             VALUES (?1, ?2, ?3, ?4, ?3)",
            params![P, next, name, quantity],
        )
        .unwrap();
        conn.last_insert_rowid()
    }

    fn set_qty(conn: &Connection, card_id: i64, q: i64) {
        conn.execute("UPDATE project_cards SET quantity = ?1 WHERE id = ?2", params![q, card_id])
            .unwrap();
    }

    fn cards_in_order(ps: &[CardPlacement]) -> Vec<i64> {
        ps.iter().map(|p| p.card_id).collect()
    }

    fn assert_dense(ps: &[CardPlacement]) {
        for (i, p) in ps.iter().enumerate() {
            assert_eq!(p.position, i as i64);
        }
    }

    #[test]
    fn seeds_in_sort_order_with_copies_together() {
        let conn = conn();
        let a = add_card(&conn, "A", 2);
        let b = add_card(&conn, "B", 1);
        let c = add_card(&conn, "C", 3);
        let ps = reconcile(&conn, P, &[]).unwrap();
        assert_eq!(cards_in_order(&ps), vec![a, a, b, c, c, c]);
        assert_dense(&ps);
        // A second read changes nothing.
        assert_eq!(reconcile(&conn, P, &[]).unwrap(), ps);
    }

    #[test]
    fn new_copies_land_after_their_siblings_and_new_cards_at_the_end() {
        let conn = conn();
        let a = add_card(&conn, "A", 2);
        let b = add_card(&conn, "B", 1);
        let ps = reconcile(&conn, P, &[]).unwrap();
        // Custom order: A, B, A.
        set_order(&conn, P, &[ps[0].id, ps[2].id, ps[1].id]).unwrap();
        set_qty(&conn, a, 3);
        let c = add_card(&conn, "C", 1);
        let ps = reconcile(&conn, P, &[]).unwrap();
        assert_eq!(cards_in_order(&ps), vec![a, b, a, a, c]);
        assert_dense(&ps);
    }

    #[test]
    fn surplus_copies_drop_from_the_end() {
        let conn = conn();
        let a = add_card(&conn, "A", 3);
        let b = add_card(&conn, "B", 1);
        let ps = reconcile(&conn, P, &[]).unwrap();
        // A, B, A, A
        set_order(&conn, P, &[ps[0].id, ps[3].id, ps[1].id, ps[2].id]).unwrap();
        let first_a = ps[0].id;
        set_qty(&conn, a, 1);
        let ps = reconcile(&conn, P, &[]).unwrap();
        assert_eq!(cards_in_order(&ps), vec![a, b]);
        assert_eq!(ps[0].id, first_a);
        assert_dense(&ps);
    }

    #[test]
    fn orphans_are_swept() {
        let conn = conn();
        let a = add_card(&conn, "A", 1);
        let b = add_card(&conn, "B", 2);
        reconcile(&conn, P, &[]).unwrap();
        conn.execute("DELETE FROM project_cards WHERE id = ?1", params![a]).unwrap();
        let ps = reconcile(&conn, P, &[]).unwrap();
        assert_eq!(cards_in_order(&ps), vec![b, b]);
        assert_dense(&ps);
    }

    #[test]
    fn set_order_refuses_a_stale_or_partial_list() {
        let conn = conn();
        add_card(&conn, "A", 2);
        let ps = reconcile(&conn, P, &[]).unwrap();
        assert!(set_order(&conn, P, &[ps[0].id]).is_err());
        assert!(set_order(&conn, P, &[ps[0].id, ps[0].id]).is_err());
        assert!(set_order(&conn, P, &[ps[0].id, ps[1].id, 9999]).is_err());
        assert!(set_order(&conn, P, &[ps[1].id, ps[0].id]).is_ok());
    }

    #[test]
    fn remove_copy_takes_the_one_pointed_at() {
        let conn = conn();
        let a = add_card(&conn, "A", 3);
        let b = add_card(&conn, "B", 1);
        let ps = reconcile(&conn, P, &[]).unwrap();
        // A1, B, A2, A3 — remove A2.
        set_order(&conn, P, &[ps[0].id, ps[3].id, ps[1].id, ps[2].id]).unwrap();
        let after = remove_copy(&conn, ps[1].id).unwrap();
        assert_eq!(after.iter().map(|p| p.id).collect::<Vec<_>>(), vec![ps[0].id, ps[3].id, ps[2].id]);
        assert_eq!(cards_in_order(&after), vec![a, b, a]);
        let q: i64 = conn
            .query_row("SELECT quantity FROM project_cards WHERE id = ?1", params![a], |r| r.get(0))
            .unwrap();
        assert_eq!(q, 2);
        assert_dense(&after);
    }

    #[test]
    fn removing_the_last_copy_removes_the_card() {
        let conn = conn();
        let a = add_card(&conn, "A", 1);
        let b = add_card(&conn, "B", 1);
        let ps = reconcile(&conn, P, &[]).unwrap();
        let after = remove_copy(&conn, ps[0].id).unwrap();
        assert_eq!(cards_in_order(&after), vec![b]);
        let left: i64 = conn
            .query_row("SELECT COUNT(*) FROM project_cards WHERE id = ?1", params![a], |r| r.get(0))
            .unwrap();
        assert_eq!(left, 0);
    }

    fn rows(ps: &[CardPlacement]) -> Vec<(i64, i64)> {
        ps.iter().map(|p| (p.card_id, p.face)).collect()
    }

    const F: i64 = FACE_FRONT;
    const T: i64 = FACE_TRANSFORM;

    #[test]
    fn first_transforms_go_beside_their_fronts() {
        let conn = conn();
        let a = add_card(&conn, "A", 2);
        let b = add_card(&conn, "B", 1);
        let ps = reconcile(&conn, P, &[]).unwrap();
        // Custom order: A, B, A — then A turns out to be double-faced.
        set_order(&conn, P, &[ps[0].id, ps[2].id, ps[1].id]).unwrap();
        let ps = reconcile(&conn, P, &[a]).unwrap();
        assert_eq!(rows(&ps), vec![(a, F), (a, T), (b, F), (a, F), (a, T)]);
        assert_dense(&ps);
        // Stable after that, with or without the list.
        assert_eq!(reconcile(&conn, P, &[a]).unwrap(), ps);
        assert_eq!(reconcile(&conn, P, &[]).unwrap(), ps);
    }

    #[test]
    fn a_new_double_faced_card_seeds_in_pairs_at_the_end() {
        let conn = conn();
        let a = add_card(&conn, "A", 1);
        let b = add_card(&conn, "B", 2);
        let ps = reconcile(&conn, P, &[b]).unwrap();
        assert_eq!(rows(&ps), vec![(a, F), (b, F), (b, T), (b, F), (b, T)]);
    }

    #[test]
    fn transforms_survive_a_short_list() {
        let conn = conn();
        let a = add_card(&conn, "A", 1);
        let b = add_card(&conn, "B", 1);
        let ps = reconcile(&conn, P, &[a]).unwrap();
        // Transform of A moved to the end.
        set_order(&conn, P, &[ps[0].id, ps[2].id, ps[1].id]).unwrap();
        let ps = reconcile(&conn, P, &[]).unwrap();
        assert_eq!(rows(&ps), vec![(a, F), (b, F), (a, T)]);
    }

    #[test]
    fn quantity_changes_move_transforms_with_their_fronts() {
        let conn = conn();
        let a = add_card(&conn, "A", 2);
        let b = add_card(&conn, "B", 1);
        let ps = reconcile(&conn, P, &[a]).unwrap();
        assert_eq!(rows(&ps), vec![(a, F), (a, T), (a, F), (a, T), (b, F)]);
        set_qty(&conn, a, 3);
        let ps = reconcile(&conn, P, &[]).unwrap();
        assert_eq!(
            rows(&ps),
            vec![(a, F), (a, T), (a, F), (a, T), (a, F), (a, T), (b, F)]
        );
        set_qty(&conn, a, 1);
        let ps = reconcile(&conn, P, &[]).unwrap();
        assert_eq!(rows(&ps), vec![(a, F), (a, T), (b, F)]);
        assert_dense(&ps);
    }

    #[test]
    fn removing_a_front_copy_takes_a_transform_with_it() {
        let conn = conn();
        let a = add_card(&conn, "A", 2);
        let ps = reconcile(&conn, P, &[a]).unwrap();
        let after = remove_copy(&conn, ps[0].id).unwrap();
        assert_eq!(rows(&after), vec![(a, T), (a, F)]);
    }

    #[test]
    fn set_order_puts_left_out_transforms_back_beside_their_fronts() {
        let conn = conn();
        let a = add_card(&conn, "A", 2);
        let b = add_card(&conn, "B", 1);
        let ps = reconcile(&conn, P, &[a]).unwrap();
        // ps: A1 A1t A2 A2t B. Send only fronts, as B, A2, A1.
        set_order(&conn, P, &[ps[4].id, ps[2].id, ps[0].id]).unwrap();
        let after = reconcile(&conn, P, &[]).unwrap();
        // The card's first transform follows the card's first front in the
        // new order, its second the second.
        assert_eq!(
            after.iter().map(|p| p.id).collect::<Vec<_>>(),
            vec![ps[4].id, ps[2].id, ps[1].id, ps[0].id, ps[3].id]
        );
        assert_eq!(rows(&after), vec![(b, F), (a, F), (a, T), (a, F), (a, T)]);
    }

    #[test]
    fn set_order_still_needs_every_front() {
        let conn = conn();
        let a = add_card(&conn, "A", 1);
        add_card(&conn, "B", 1);
        let ps = reconcile(&conn, P, &[a]).unwrap();
        // A, At, B: leaving out B is stale, leaving out At is fine.
        assert!(set_order(&conn, P, &[ps[0].id, ps[1].id]).is_err());
        assert!(set_order(&conn, P, &[ps[2].id, ps[0].id]).is_ok());
    }
}

-- 008_eval_keys — make dataset import idempotent.
--
-- A dataset file is declarative: you edit a question's expected ids, re-import,
-- and expect the row to be updated. Without a key to upsert on, the only way to
-- do that is delete-then-insert — and eval_results.question_id cascades on
-- delete, so re-importing would silently erase the per-question history of
-- every run ever recorded. Those rows are the whole point of keeping runs
-- immutable.
--
-- md5(question) rather than the text itself: a btree index has a row-size limit
-- and a question is free-form prose that can exceed it. Collisions are not a
-- practical concern for a hand-labelled set, and the consequence of one would
-- be an update to the wrong row in a dataset file the author controls, not a
-- security boundary.

create unique index if not exists eval_questions_dataset_question
  on eval_questions (dataset_id, md5(question));

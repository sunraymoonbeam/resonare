# app/jobs/tasks.py
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import boto3
import hydra
import requests
from pydantic import ValidationError

from .models import Block, Chat, Message
from .utils import (
    calculate_chat_stats,
    load_tokenizer,
    parse_date_limit,
)

logger = logging.getLogger(__name__)


def run_data_processing(
    run_id: str,
    raw_json_path: str,
    s3_client: boto3.client,
    s3_bucket_name: str,
    overrides: Dict[str, Any],
) -> Dict[str, Any]:
    """End-to-end preprocessing worker.

    Args:
        run_id (str): UUID string identifying this job.
        raw_json_path (str): Path to the temporary JSON file containing raw chat data.
        s3_client (boto3.client): Authenticated Boto3 S3 client.
        s3_bucket_name (str): Name of the S3 bucket for uploading results.
        overrides (Dict[str, Any]): Configuration overrides (e.g., thresholds, names).

    Returns:
        Dict[str, Any]: A dictionary of summary statistics, for example:
            {
                "num_chats": 12,
                "num_blocks": 345,
                "avg_tokens_per_block": 150.2,
                ...
            }

    Raises:
        FileNotFoundError: If the raw JSON file cannot be found.
        RuntimeError: For any S3 upload/download or processing errors.
    """
    # Load configuration
    with hydra.initialize(config_path="../../../conf"):
        cfg = hydra.compose(config_name="config")

    # Apply overrides
    logger.info(f"Applying configuration overrides: {overrides}")
    override_counts = {
        "main": 0,
        "dataset": 0,
        "lora": 0,
        "model": 0,
        "training": 0,
        "skipped": 0,
    }
    logger.info(f"cfg.fine_tuning.dataset: {cfg.fine_tuning.dataset}")
    logger.info(f"cfg.fine_tuning.lora: {cfg.fine_tuning.lora}")
    logger.info(f"cfg.fine_tuning.model: {cfg.fine_tuning.model}")
    logger.info(f"cfg.fine_tuning.training: {cfg.fine_tuning.training}")
    logger.info(f"cfg: {cfg}")
    for key, value in overrides.items():
        if hasattr(cfg, key):  # Check if the attribute exists in cfg
            logger.debug(f"Applying main config override: {key}={value}")
            setattr(cfg, key, value)
            override_counts["main"] += 1
        elif hasattr(cfg.fine_tuning.dataset, key):
            logger.debug(f"Applying dataset override: {key}={value}")
            setattr(cfg.fine_tuning.dataset, key, value)
            override_counts["dataset"] += 1
        elif hasattr(cfg.fine_tuning.lora, key):
            logger.debug(f"Applying LoRA override: {key}={value}")
            setattr(cfg.fine_tuning.lora, key, value)
            override_counts["lora"] += 1
        elif hasattr(cfg.fine_tuning.model, key):
            logger.debug(f"Applying model override: {key}={value}")
            setattr(cfg.fine_tuning.model, key, value)
            override_counts["model"] += 1
        elif hasattr(cfg.fine_tuning.training, key):
            logger.debug(f"Applying training override: {key}={value}")
            setattr(cfg.fine_tuning.training, key, value)
            override_counts["training"] += 1
        else:
            logger.warning(f"Override skipped: '{key}' not found in configuration.")
            override_counts["skipped"] += 1

    logger.info(
        f"Override summary - Main: {override_counts['main']}, Dataset: {override_counts['dataset']}, "
        f"LoRA: {override_counts['lora']}, Model: {override_counts['model']}, "
        f"Training: {override_counts['training']}, Skipped: {override_counts['skipped']}"
    )

    # --------------------------------------------------------------------
    # 1) Load raw chats from temp file, then delete the file when done
    # --------------------------------------------------------------------
    path = Path(raw_json_path)
    raw_chats: List[Dict] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))

        if isinstance(data, list):
            raw_chats = [c for c in data if {"name", "messages"} <= c.keys()]

        elif isinstance(data, dict) and "chats" in data and "list" in data["chats"]:
            raw_chats = data["chats"]["list"]

        elif isinstance(data, dict) and {"name", "messages"} <= data.keys():
            raw_chats = [data]

        else:
            raise ValueError("Unrecognised JSON structure")

        if not raw_chats:
            raise ValueError("List contained no valid chat objects")

    except Exception as e:
        logger.error(f"Failed to load raw chats from {path}: {e}")
        raise

    finally:
        path.unlink(missing_ok=True)  # always remove temp file

    logger.info("Loaded %s raw chats from %s", len(raw_chats), path)

    # -------------------------------
    # 2) Export: Raw Chats - local / s3
    # -------------------------------
    # Define base paths
    base_dir = Path(cfg.output.local_dir)
    run_dir = base_dir / run_id

    # Save locally if configured
    if "local" in cfg.output.modes:
        logger.info(f"Saving raw chats locally to {run_dir}...")

        run_dir.mkdir(parents=True, exist_ok=True)
        raw_chats_filepath = run_dir / "raw.json"

        with raw_chats_filepath.open("w", encoding="utf-8") as f:
            json.dump(raw_chats, f, ensure_ascii=False, indent=2)

    # Upload to S3
    logger.info(f"Uploading raw chats to S3 bucket {cfg.output.s3_bucket}...")

    try:
        s3_client.put_object(
            Bucket=cfg.output.s3_bucket,
            Key=f"{run_id}/data/raw.json",
            Body=json.dumps(raw_chats, ensure_ascii=False, indent=2),
            Metadata={
                "uuid": run_id,
            },
        )
        logger.info(
            f"Successfully uploaded raw chats to s3://{cfg.output.s3_bucket}/{run_id}/raw.json"
        )
    except Exception as e:
        logger.error(f"Failed to upload raw chats to S3: {e}")
        raise

    # ---------------------
    # 3) Tokenizer loading
    # ---------------------
    # We need a tokenizer to split chat messages into tokens and obtain token counts, for chunking and filtering:
    #  - Prefer the same tokenizer family (e.g. BPE, SentencePiece, WordPiece) as our target finetuning model for accuracy.
    #  - Use HuggingFace’s AutoTokenizer to load the specific tokenizer for the target model.
    #  - If that fails, fall back to OpenAI’s tiktoken (BPE) for speed and API‑compatibility.
    logger.info(
        f"Loading tokenizer for model {cfg.fine_tuning.model.name} for token counting..."
    )
    tokenizer = load_tokenizer(model_name=cfg.fine_tuning.model.name)

    # --------------------------------------------
    # 4) Build Chat objects
    # --------------------------------------------
    # We assemble a list of Chat instances, each representing a chat:
    #  - contact_name: the person or group name

    #  - chat_type: one‑on‑one, group, or supergroup

    #  - messages: a flat, chronological list of Message objects, each with:
    #    - role: the sender of the message (user or system)
    #    - content: the message text
    #    - timestamp: the datetime when the message was sent

    #  - blocks: A list of message blocks. Each block is a list of temporally
    #            and contextually related messages, chunked according to time
    #            and token limits. Defaults to an empty list.
    logger.info("Building chat objects from raw chats...")

    chats: List[Chat] = []
    target_name = cfg.target_name  # Name identifying "our" side of the conversation, renamed to "assistant" in the output
    date_limit = parse_date_limit(
        cfg.date_limit  # Optional date limit for filtering messages
    )

    for chat in raw_chats:
        contact_name = chat.get("name")

        if not contact_name:  # Skip chats without a name (deleted/anonymous)
            continue

        chat_type = chat.get("type")
        # Currently limiting to personal chats.
        # TODO: Expand this list or logic if group chat support is added @renhwa.
        # Potential Issue: Group chats seem to be a little wonky, only includes target name and messages.
        if chat_type not in ["personal_chat"]:
            continue

        msgs: List[Message] = []
        for msg in chat.get("messages", []):
            try:
                sender = msg.get("from", "")
                ents = msg.get("text_entities", [])
                sticker = msg.get("sticker_emoji", "")

                # We need a sender and some form of text content (entities or sticker).
                # We only include text_entities and sticker_emoji, since those produce tokenizable text, and skip other media (photos, files, voice notes
                if not sender or (not ents and not sticker):
                    continue

                # Reconstruct the textual content from entities + emoji.
                raw_text = "".join(ent["text"] for ent in ents) + sticker
                # Remove leading/trailing whitespace and replace internal newlines with spaces.
                # Newlines will be used later to delimit merged messages.
                content = raw_text.strip().replace("\n", " ")

                # Parse timestamp and apply date filter if set.
                timestamp = datetime.fromisoformat(msg["date"])
                if date_limit and timestamp < date_limit:
                    continue

                msgs.append(
                    Message(
                        role="assistant"  # Assign role based on sender
                        if sender == target_name
                        else "user",  # Assign sender role based on target_name
                        content=content,
                        timestamp=timestamp,
                    )
                )

            except Exception as e:
                logger.warning(
                    f"[{contact_name}] skipping a message due to parse error: {e}"
                )

        # If we found any valid messages, construct and append the Conversation object.
        if msgs:
            msgs.sort(
                key=lambda m: m.timestamp
            )  # Sort messages by timestamp to ensure chronological order.

            try:
                # Create a new Chat object with the parsed messages
                chat = Chat(
                    contact_name=contact_name,
                    type=chat_type,
                    messages=msgs,
                )
                chats.append(chat)
            except ValidationError as e:
                logger.warning(
                    f"Failed to create chat object for '{contact_name}': {e}"
                )
                continue

    logger.info(f"Built {len(chats)} usable chat objects.")

    # -------------------------------
    # 5) Chunking each chat into conversation 'blocks'
    # -------------------------------
    # Split each Chat.messages into “blocks” so that each block:
    #   • Maintains temporal context (messages no more than time_threshold_sec apart)
    #   • Stays within a token-budget (min_tokens ≤ block_tokens ≤ max_tokens)
    # This ensures that during LLM training each example has coherent context, and is neither too short (unhelpful) nor too long (slow to train on).

    convo_thereshold_secs = cfg.convo_block_thereshold_secs
    min_tokens = cfg.min_tokens_per_block
    max_tokens = cfg.max_tokens_per_block

    # Sanity‑checks
    if min_tokens >= max_tokens:
        logger.warning(
            f"Invalid token thresholds: min_tokens ({min_tokens}) ≥ max_tokens ({max_tokens}). "
            "Resetting to defaults: min_tokens=100, max_tokens=3000."
        )
        min_tokens, max_tokens = 100, 3000

    # variables to track block counts
    num_short_blocks = 0
    num_long_blocks = 0

    logger.info("Chunking chats into blocks...")
    for chat in chats:
        current_block: List[Message] = []
        current_tokens = 0
        previous_time: Optional[datetime] = None

        for msg in chat.messages:
            gap = (
                (msg.timestamp - previous_time).total_seconds()
                if previous_time
                else None
            )
            msg_tokens = len(tokenizer.encode(msg.content))

            # Continue block if within time and token limits
            if (
                previous_time
                and gap <= convo_thereshold_secs
                and (current_tokens + msg_tokens) <= max_tokens
            ):
                current_block.append(msg)
                current_tokens += msg_tokens
            else:
                # Commit the existing block
                if current_block:
                    if min_tokens <= current_tokens <= max_tokens:
                        chat.raw_blocks.append(current_block)
                    elif current_tokens < min_tokens:
                        num_short_blocks += 1
                    else:
                        num_long_blocks += 1

                # Start a new block
                current_block = [msg]
                current_tokens = msg_tokens

            previous_time = msg.timestamp

        # Commit any remaining block
        if current_block:
            if min_tokens <= current_tokens <= max_tokens:
                chat.raw_blocks.append(current_block)
            elif current_tokens < min_tokens:
                num_short_blocks += 1
            else:
                num_long_blocks += 1

    # Discard empty chats with empty blocks
    num_original_chats = len(chats)
    chats = [c for c in chats if c.raw_blocks]
    num_discarded_chats = num_original_chats - len(chats)

    # Log the results
    num_total_blocks = sum(len(c.raw_blocks) for c in chats)
    logger.info(
        f"Chunking complete: {num_original_chats} conversations → {len(chats)} conversations ({num_discarded_chats} discarded due to empty blocks), "
        f"{num_total_blocks} chat blocks created; discarded short {num_short_blocks} blocks and {num_long_blocks} long blocks."
    )

    # -------------------------------
    # 6) Merge consecutive messages by sender within each block
    # -------------------------------
    # For each block in each Conversation, we:
    #   • Group consecutive messages from the same sender into one Message
    #   • Prefix every line with the delimiter (e.g. '>>>')
    #   • Separate lines with '\n'
    #   • Keep the timestamp of the first message in each group
    #   • For each block, trim leading assistant messages and trailing user messages
    #   • Add a system message at the start of each block if specified
    logger.info("Merging consecutive messages by sender within each block...")
    delimiter = cfg.message_delimiter.strip()

    for chat in chats:
        merged_blocks: List[List[Message]] = []

        for block in chat.raw_blocks:
            current_block: List[Message] = []

            first_msg = block[0]
            current_sender = first_msg.role
            current_timestamp = first_msg.timestamp
            current_content = f"{delimiter} {first_msg.content.strip()}"

            for msg in block[1:]:
                if (
                    msg.role == current_sender
                ):  # concatenate messages from the same sender
                    current_content += f"\n{delimiter} {msg.content.strip()}"
                else:
                    try:
                        # Create and add the merged message to the list
                        current_block.append(
                            Message(
                                role=current_sender,
                                content=current_content,
                                timestamp=current_timestamp,
                            )
                        )
                    except ValidationError as e:
                        logger.warning(
                            f"Failed to create merged message for chat '{chat.contact_name}', "
                            f"block starting at {block[0].timestamp if block else 'unknown'}: {e}"
                        )
                        continue

                    current_sender = msg.role
                    current_timestamp = msg.timestamp
                    current_content = f"{delimiter} {msg.content.strip()}"

            # Add last merged message if exists
            if current_content:
                try:
                    # Create and add the merged message to the list
                    current_block.append(
                        Message(
                            role=current_sender,
                            content=current_content,
                            timestamp=current_timestamp,
                        )
                    )
                except ValidationError as e:
                    logger.warning(
                        f"Failed to create merged message for chat '{chat.contact_name}', "
                        f"block starting at {block[0].timestamp if block else 'unknown'}: {e}"
                    )
                    continue

            merged_blocks.append(current_block)

        chat.raw_blocks = merged_blocks

    # ------------------------------------------------------------------
    # 6b) Ensure each block starts with SYSTEM (if specified), USER and ends with ASSISTANT
    # ------------------------------------------------------------------
    discarded_short_blocks = 0
    discarded_long_blocks = 0
    system_message = None

    if cfg.system_prompt:
        logger.info(
            f"Prepending system message to each conversation block with content: {cfg.system_prompt}"
        )
        # Try to build the system message first
        try:
            system_message = Message(
                role="system",
                content=cfg.system_prompt,
                timestamp=None,
            )
        except Exception as e:
            logger.error(
                f"Failed to create system message, skipping system prompts: {e}"
            )

    for chat in chats:
        valid_blocks: List[Block] = []

        for block in chat.raw_blocks:
            if not block:
                continue

            # Trim leading assistant messages
            while block and block[0].role == "assistant":
                block.pop(0)

            # Trim trailing user messages
            while block and block[-1].role == "user":
                block.pop()

            # Add a system message if specified
            if system_message:
                block.insert(0, system_message)

            # structural length check
            min_msgs = 3 if system_message else 2
            if len(block) < min_msgs:
                discarded_short_blocks += 1
                continue

            # token‐count check
            token_count = sum(len(tokenizer.encode(m.content)) for m in block)
            if token_count < min_tokens:
                discarded_short_blocks += 1
                continue
            elif token_count > max_tokens:
                discarded_long_blocks += 1
                continue
            discarded_blocks = discarded_short_blocks + discarded_long_blocks

            try:
                # Create a new Block object with the trimmed messages
                block = Block(messages=block)
                valid_blocks.append(block)

            except ValidationError as e:
                logger.warning(
                    f"Failed to create block for chat '{chat.contact_name}', "
                    f"block starting at {block.messages[0].timestamp if block else 'unknown'}: {e}"
                )
                discarded_blocks += 1
                continue

        chat.valid_blocks = valid_blocks

    logger.info(
        f"Role‑sanity pass complete: {sum(len(chat.valid_blocks) for chat in chats)} valid blocks kept, "
        f"total of {discarded_blocks} blocks discarded, {discarded_short_blocks} short blocks and {discarded_long_blocks} long blocks."
    )

    # -------------------------------
    # 7) Log summary statistics
    # -------------------------------
    logger.info("Calculating statistics of processed chats...")
    chat_stats = calculate_chat_stats(chats, tokenizer)

    # Define the number of top entries to display
    k = 10

    # Extract and sort the block breakdown by the number of blocks in descending order
    top_k_breakdown = sorted(
        chat_stats["block_breakdown"].items(), key=lambda item: item[1], reverse=True
    )[:k]

    stats_table = "\n"
    stats_table += "*" * 36 + "\n"
    stats_table += "*{:^34}*\n".format("Chat Statistics Summary")
    stats_table += "*" * 36 + "\n"
    stats_table += f"{'Metric':<25} | {'Value':>8}\n"
    stats_table += "-" * 36 + "\n"
    stats_table += f"{'Total Chats':<25} | {chat_stats['num_chats']:>8}\n"
    stats_table += f"{'Total Blocks':<25} | {chat_stats['num_blocks']:>8}\n"
    stats_table += (
        f"{'Min Tokens/Block':<25} | {chat_stats['min_tokens_per_block']:>8}\n"
    )
    stats_table += (
        f"{'Max Tokens/Block':<25} | {chat_stats['max_tokens_per_block']:>8}\n"
    )
    stats_table += (
        f"{'Avg Tokens/Block':<25} | {chat_stats['avg_tokens_per_block']:>8.2f}\n"
    )
    stats_table += f"{'Min Duration (min)':<25} | {chat_stats['min_duration_minutes_per_block']:>8.2f}\n"
    stats_table += f"{'Max Duration (min)':<25} | {chat_stats['max_duration_minutes_per_block']:>8.2f}\n"
    stats_table += f"{'Avg Duration (min)':<25} | {chat_stats['avg_duration_minutes_per_block']:>8.2f}\n"

    stats_table += "\n"
    stats_table += "*" * 36 + "\n"
    stats_table += "*{:^34}*\n".format("Top Chats by Block Count")
    stats_table += "*" * 36 + "\n"
    for rank, (name, count) in enumerate(top_k_breakdown, start=1):
        stats_table += f"{rank:>2}. {name:<28} {count:>5}\n"

    logger.info("\n" + stats_table)

    # -------------------------------
    # 8) Export: Processed Chats and Training Blocks
    # -------------------------------
    logger.info("Exporting processed chats and training blocks...")

    # Define paths
    processed_chats_filepath = run_dir / "processed.json"
    training_blocks_filepath = run_dir / "train.jsonl"

    # --- Manually Define Metadata ---
    metadata_dict = {
        "uuid": run_id,
        "model_id": cfg.fine_tuning.model.name,
        "target_name": cfg.target_name,
        "system_prompt": str(cfg.system_prompt) if cfg.system_prompt else "None",
        "date_limit": str(cfg.date_limit) if cfg.date_limit else "None",
        "convo_block_thereshold_secs": str(cfg.convo_block_thereshold_secs),
        "min_tokens_per_block": str(cfg.min_tokens_per_block),
        "max_tokens_per_block": str(cfg.max_tokens_per_block),
        "message_delimiter": cfg.message_delimiter,
    }
    metadata_dict.update(
        {f"stats_{k}": str(v) for k, v in chat_stats.items()}
    )  # add stats to metadata

    fine_tuning_metadata = {
        # Model settings
        "model_name": cfg.fine_tuning.model.name,
        "max_seq_length": str(cfg.fine_tuning.model.max_seq_length),
        "load_in_4bit": str(cfg.fine_tuning.model.load_in_4bit),
        "chat_template": cfg.fine_tuning.model.chat_template,
        # Dataset settings
        "dataset_split": cfg.fine_tuning.dataset.split,
        "dataset_num_proc": str(cfg.fine_tuning.dataset.num_proc),
        # LoRA settings
        "lora_r": str(cfg.fine_tuning.lora.r),
        "lora_alpha": str(cfg.fine_tuning.lora.alpha),
        "lora_dropout": str(cfg.fine_tuning.lora.dropout),
        "lora_bias": cfg.fine_tuning.lora.bias,
        "use_gradient_checkpointing": str(
            cfg.fine_tuning.lora.use_gradient_checkpointing
        ),
        "random_state": str(cfg.fine_tuning.lora.random_state),
        "use_rslora": str(cfg.fine_tuning.lora.use_rslora),
        "target_modules": str(cfg.fine_tuning.lora.target_modules),
        # Training settings
        "batch_size": str(cfg.fine_tuning.training.per_device_train_batch_size),
        "gradient_accumulation_steps": str(
            cfg.fine_tuning.training.gradient_accumulation_steps
        ),
        "warmup_steps": str(cfg.fine_tuning.training.warmup_steps),
        "max_steps": str(cfg.fine_tuning.training.max_steps),
        "learning_rate": str(cfg.fine_tuning.training.learning_rate),
        "weight_decay": str(cfg.fine_tuning.training.weight_decay),
        "lr_scheduler_type": cfg.fine_tuning.training.lr_scheduler_type,
        "seed": str(cfg.fine_tuning.training.seed),
        "packing": str(cfg.fine_tuning.training.packing),
    }

    # Update the metadata_dict with fine-tuning metadata
    metadata_dict.update({f"ft_{k}": v for k, v in fine_tuning_metadata.items()})

    # 8.1) Prepare chat records
    logger.info("Preparing processed chat records...")
    chat_records = []
    for chat in chats:
        chat_record = {
            "contact_name": chat.contact_name,
            "chat_type": chat.type,
            "num_blocks": len(chat.valid_blocks),
            "blocks": [
                {
                    "messages": [
                        {
                            "timestamp": msg.timestamp.isoformat()
                            if msg.timestamp
                            else None,
                            "role": msg.role,
                            "content": msg.content,
                        }
                        for msg in block.messages
                    ]
                }
                for block in chat.valid_blocks
            ],
        }
        chat_records.append(chat_record)

    # 8.2) Save processed chats locally if needed
    if "local" in cfg.output.modes:
        logger.info(f"Saving processed chats locally to {processed_chats_filepath}...")
        with processed_chats_filepath.open("w", encoding="utf-8") as f:
            json.dump(chat_records, f, ensure_ascii=False, indent=2)

    # 8.3) Upload processed chats to S3
    if s3_client is not None:
        logger.info(f"Uploading processed chats to S3 bucket {cfg.output.s3_bucket}...")
        try:
            s3_client.put_object(
                Bucket=cfg.output.s3_bucket,
                Key=f"{run_id}/data/processed.json",
                Body=json.dumps(chat_records, ensure_ascii=False, indent=2),
                Metadata=metadata_dict,
            )
            logger.info(
                f"Successfully uploaded chats.json to s3://{cfg.output.s3_bucket}/{run_id}/data/processed.json"
            )
        except Exception as e:
            logger.error(f"Failed to upload chats.json to S3: {e}")

    # 8.4) Save training blocks locally if needed
    logger.info("Preparing training blocks...")
    training_block_lines = []
    for chat in chats:
        for block in chat.valid_blocks:
            record = {
                "messages": [
                    {"role": msg.role, "content": msg.content} for msg in block.messages
                ]
            }
            training_block_lines.append(
                json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            )

    if "local" in cfg.output.modes:
        logger.info(f"Saving training blocks locally to {training_blocks_filepath}...")
        with training_blocks_filepath.open("w", encoding="utf-8") as f:
            f.write("\n".join(training_block_lines))

    # 8.5) Upload training blocks to S3
    if s3_client is not None:
        logger.info(f"Uploading training blocks to S3 bucket {cfg.output.s3_bucket}...")
        try:
            s3_client.put_object(
                Bucket=cfg.output.s3_bucket,
                Key=f"{run_id}/data/train.jsonl",
                Body="\n".join(training_block_lines),
                Metadata=metadata_dict,
            )
            logger.info(
                f"Successfully uploaded train.jsonl to s3://{cfg.output.s3_bucket}/{run_id}/data/train.jsonl"
            )
        except Exception as e:
            logger.error(f"Failed to upload train.jsonl to S3: {e}")

    # -------------------------------
    # 9) Send request to fine-tuning service to start the training job
    # -------------------------------
    fine_tuning_url = os.getenv("FINE_TUNING_SERVICE_URL")

    if not fine_tuning_url:
        logger.error("FINE_TUNING_SERVICE_URL environment variable is not set.")
        return chat_stats

    # Send request to start fine-tuning
    try:
        response = requests.post(
            fine_tuning_url,
            json={"run_id": run_id},
            headers={"Content-Type": "application/json"},
        )

        if response.status_code == 200:
            logger.info(f"Successfully queued fine-tuning job: {response.json()}")
        else:
            logger.error(
                f"Failed to queue fine-tuning job: {response.status_code} - {response.text}"
            )

    except Exception as e:
        logger.error(f"Error sending fine-tuning request: {e}")

    return chat_stats

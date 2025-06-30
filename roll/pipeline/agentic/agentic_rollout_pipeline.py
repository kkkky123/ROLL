import json
import os.path
from typing import Any

import ray
import torch
from codetiming import Timer

from roll.agentic.rollout.rollout_scheduler import RolloutScheduler
from roll.distributed.executor.cluster import Cluster
from roll.distributed.scheduler.protocol import DataProto
from roll.models.model_providers import default_tokenizer_provider
from roll.pipeline.agentic.agentic_config import AgenticConfig
from roll.pipeline.agentic.utils import dump_rollout_render
from roll.pipeline.base_pipeline import BasePipeline
from roll.utils.functionals import (
    reduce_metrics,
)
from roll.utils.logging import get_logger

logger = get_logger()


class AgenticRolloutPipeline(BasePipeline):
    """
    this is just for env rollout
    """
    def __init__(self, pipeline_config: AgenticConfig):
        super().__init__(pipeline_config)
        self.pipeline_config: AgenticConfig

        self.pipeline_config.set_max_steps(max_steps=self.pipeline_config.max_steps)

        self.tokenizer = default_tokenizer_provider(model_args=self.pipeline_config.actor_train.model_args)

        self.actor_infer: Any = Cluster(
            name=self.pipeline_config.actor_infer.name,
            worker_cls=self.pipeline_config.actor_infer.worker_cls,
            resource_manager=self.resource_manager,
            worker_config=self.pipeline_config.actor_infer,
        )

        self.rollout_scheduler = RolloutScheduler(
            config=self.pipeline_config,
            env_manager_config=self.pipeline_config.train_env_manager,
            resource_manager=self.resource_manager,
            infer_cluster=self.actor_infer,
            mode="train",
        )

        self.actor_infer.initialize(pipeline_config=self.pipeline_config, blocking=True)

    @torch.no_grad()
    def run(self):

        for global_step in range(self.pipeline_config.max_steps):
            logger.info(f"pipeline rollout global step {global_step} start...")
            metrics = {}
            batch: DataProto = DataProto()
            batch.meta_info = {"global_step": global_step}

            with Timer(name="rollout", logger=None) as rollout_timer:
                batch.meta_info["is_offload_states"] = True
                batch = self.rollout_scheduler.get_batch(batch, self.pipeline_config.rollout_batch_size)
                if self.pipeline_config.render_save_dir:
                    self.executor.submit(
                        dump_rollout_render,
                        save_dir=self.pipeline_config.render_save_dir,
                        step=global_step,
                        frames=batch.non_tensor_batch["frames"],
                        env_ids=batch.non_tensor_batch["env_ids"],
                        tags=batch.non_tensor_batch["tags"],
                        episode_scores=batch.non_tensor_batch["episode_scores"],
                    )
            metrics["time/rollout"] = rollout_timer.last
            metrics.update(reduce_metrics(batch.meta_info.pop("metrics", {})))
            scores = batch.batch["scores"].sum(-1)
            metrics["rollout/score/mean"] = torch.mean(scores).detach().item()
            metrics["rollout/score/max"] = torch.max(scores).detach().item()
            metrics["rollout/score/min"] = torch.min(scores).detach().item()
            batch.meta_info["global_step"] = global_step
            metrics["system/samples"] = (global_step + 1) * batch.batch.shape[0]

            self.tracker.log(values=metrics, step=global_step)

            if global_step % self.pipeline_config.logging_steps == 0:
                if int(os.environ.get("RAY_PROFILING", "0")):
                    timeline_dir = os.path.join(self.pipeline_config.profiler_output_dir, "timeline")
                    os.makedirs(timeline_dir, exist_ok=True)
                    ray.timeline(
                        filename=os.path.join(timeline_dir, f"timeline-step-{global_step}.json"),
                    )

                prompt_mask = batch.batch["prompt_mask"]
                non_prompt_mask = batch.batch["non_prompt_mask"]
                input_ids = batch.batch["input_ids"]
                prompt_ids = torch.where(
                    prompt_mask.bool(), input_ids, torch.full_like(input_ids, self.tokenizer.pad_token_id)
                )
                response_ids = torch.where(
                    non_prompt_mask.bool(), input_ids, torch.full_like(input_ids, self.tokenizer.pad_token_id)
                )

                generate_res = []
                prompts = self.tokenizer.batch_decode(prompt_ids, skip_special_tokens=True)
                responses = self.tokenizer.batch_decode(response_ids, skip_special_tokens=True)
                episode_scores = batch.non_tensor_batch["episode_scores"].tolist()
                llm_raw_text_list = batch.non_tensor_batch["llm_raw_text_list"].tolist()
                for prompt, prompt_id, response, response_id, episode_score, llm_raw_text in zip(
                    prompts, prompt_ids, responses, response_ids, episode_scores, llm_raw_text_list
                ):
                    generate_res.append(
                        {
                            "prompt": prompt,
                            "response": response,
                            "episode_score": episode_score,
                            "llm_raw_text": llm_raw_text,
                        }
                    )
                logger.info(json.dumps(generate_res[:10], ensure_ascii=False))
                logger.info(json.dumps(metrics, ensure_ascii=False))

            logger.info(f"pipeline step {global_step} finished")
            global_step += 1
            logger.info(f"epoch {global_step} finished")
        logger.info("pipeline complete!")

import os
import pickle
from collections import OrderedDict, defaultdict

import numpy as np
from tqdm import tqdm

from util.logger import logger


class ReplayBuffer:
    def __init__(self, config, keys, sample_func):
        """
        Stores by key, value is list of episodes.
        To get Episode 100's ob, do buffer['ob'][100]
        """
        self._config = config
        self._size = config.buffer_size
        self._sample_func = sample_func

        # create the buffer to store info
        self._keys = keys
        self.clear()

    def clear(self):
        self._idx = 0
        self._current_size = 0
        self._new_episode = True
        self._buffer = defaultdict(list)

    # store the episode
    def store_episode(self, rollout):
        if self._new_episode:
            for k in self._keys:
                if self._current_size < self._size:
                    self._buffer[k].append(rollout[k])
                else:
                    self._buffer[k][self._idx] = rollout[k]
        else:
            for k in self._keys:
                if k == "ob":
                    self._buffer[k][self._idx].append(rollout[k][1:])
                else:
                    self._buffer[k][self._idx].append(rollout[k])
        if "ac" in self._buffer:
            assert (
                len(self._buffer["ob"][self._idx])
                == len(self._buffer["ac"][self._idx]) + 1
            )
        if rollout["done"][-1]:
            self._idx = (self._idx + 1) % self._size
            self._current_size += 1
            self._new_episode = True

    # sample the data from the replay buffer
    def sample(self, batch_size):
        # sample transitions
        transitions = self._sample_func(self._buffer, batch_size)
        return transitions

    def state_dict(self):
        return self._buffer

    def load_state_dict(self, state_dict):
        self._buffer = state_dict
        self._current_size = len(self._buffer["ac"])

    def load_demonstrations(self, demo_folder, num_demos):
        """
        Loads demo files and adds them into the buffer
        """
        demos = []
        i = 0
        for d in os.scandir(demo_folder):
            if d.is_file() and d.path.endswith("pkl"):
                demos.append(d.path)
                i += 1
            if i > num_demos:
                break
        d = "Loading demos"
        loader = tqdm(demos, desc=d) if self._config.is_chef else demos
        for path in loader:
            with open(path, "rb") as f:
                data = pickle.load(f)
                rollout = {}
                rollout["ob"] = data["obs"]
                rollout["ac"] = [OrderedDict(default=ac) for ac in data["actions"]]
                rollout["rew"] = data["rewards"]
                rollout["done"] = np.zeros(len(data["actions"]))
                rollout["done"][-1] = 1
                self.store_episode(rollout)


class LearnedRewardReplayBuffer(ReplayBuffer):
    def __init__(self, config, keys, sample_func, rew):
        super().__init__(config, keys, sample_func)
        self._rew = rew

    def sample(self, batch_size_in_transitions):
        # sample transitions
        episode_batch = self._buffer
        rollout_batch_size = len(episode_batch["ac"])
        batch_size = batch_size_in_transitions

        episode_idxs = np.random.randint(0, rollout_batch_size, batch_size)
        t_samples = [
            np.random.randint(len(episode_batch["ac"][episode_idx]))
            for episode_idx in episode_idxs
        ]

        transitions = {}
        for key in episode_batch.keys():
            transitions[key] = [
                episode_batch[key][episode_idx][t]
                for episode_idx, t in zip(episode_idxs, t_samples)
            ]

        transitions["ob_next"] = [
            episode_batch["ob"][episode_idx][t + 1]
            for episode_idx, t in zip(episode_idxs, t_samples)
        ]

        intr_rew = self._rew(transitions["ob"], transitions["ob_next"])
        transitions["rew"] = np.array(transitions["env_rew"]) + intr_rew

        new_transitions = {}
        for k, v in transitions.items():
            if isinstance(v[0], dict):
                sub_keys = v[0].keys()
                new_transitions[k] = {
                    sub_key: np.stack([v_[sub_key] for v_ in v]) for sub_key in sub_keys
                }
            else:
                new_transitions[k] = np.stack(v)

        return new_transitions

    def load_demonstrations(self, demo_folder):
        """
        Loads demo files and adds them into the buffer
        """
        demos = [
            d.path
            for d in os.scandir(demo_folder)
            if d.is_file() and d.path.endswith("pkl")
        ]
        for path in demos:
            with open(path, "rb") as f:
                data = pickle.load(f)
                rollout = {}
                rollout["ob"] = data["obs"]
                rollout["ac"] = [OrderedDict(default=ac) for ac in data["actions"]]
                rollout["env_rew"] = data["rewards"]
                rollout["done"] = np.zeros(len(data["actions"]))
                rollout["done"][-1] = 1
                self.store_episode(rollout)
        logger.info(f"Loaded {len(demos)} demos into buffer.")


class RandomSampler:
    def sample_func(self, episode_batch, batch_size_in_transitions):
        rollout_batch_size = len(episode_batch["ac"])
        batch_size = batch_size_in_transitions

        episode_idxs = np.random.randint(0, rollout_batch_size, batch_size)
        t_samples = [
            np.random.randint(len(episode_batch["ac"][episode_idx]))
            for episode_idx in episode_idxs
        ]

        transitions = {}
        for key in episode_batch.keys():
            transitions[key] = [
                episode_batch[key][episode_idx][t]
                for episode_idx, t in zip(episode_idxs, t_samples)
            ]

        transitions["ob_next"] = [
            episode_batch["ob"][episode_idx][t + 1]
            for episode_idx, t in zip(episode_idxs, t_samples)
        ]

        new_transitions = {}
        for k, v in transitions.items():
            if isinstance(v[0], dict):
                sub_keys = v[0].keys()
                new_transitions[k] = {
                    sub_key: np.stack([v_[sub_key] for v_ in v]) for sub_key in sub_keys
                }
            else:
                new_transitions[k] = np.stack(v)

        return new_transitions


class HERSampler:
    def __init__(self, replay_strategy, replace_future, reward_func=None):
        self.replay_strategy = replay_strategy
        if self.replay_strategy == "future":
            self.future_p = replace_future
        else:
            self.future_p = 0
        self.reward_func = reward_func

    def sample_her_transitions(self, episode_batch, batch_size_in_transitions):
        rollout_batch_size = len(episode_batch["ac"])
        batch_size = batch_size_in_transitions

        # select which rollouts and which timesteps to be used
        episode_idxs = np.random.randint(0, rollout_batch_size, batch_size)
        t_samples = [
            np.random.randint(len(episode_batch["ac"][episode_idx]))
            for episode_idx in episode_idxs
        ]

        transitions = {}
        for key in episode_batch.keys():
            transitions[key] = [
                episode_batch[key][episode_idx][t]
                for episode_idx, t in zip(episode_idxs, t_samples)
            ]

        transitions["ob_next"] = [
            episode_batch["ob"][episode_idx][t + 1]
            for episode_idx, t in zip(episode_idxs, t_samples)
        ]
        transitions["r"] = np.zeros((batch_size,))

        # hindsight experience replay
        for i, (episode_idx, t) in enumerate(zip(episode_idxs, t_samples)):
            replace_goal = np.random.uniform() < self.future_p
            if replace_goal:
                future_t = np.random.randint(
                    t + 1, len(episode_batch["ac"][episode_idx]) + 1
                )
                future_ag = episode_batch["ag"][episode_idx][future_t]
                if (
                    self.reward_func(
                        episode_batch["ag"][episode_idx][t], future_ag, None
                    )
                    < 0
                ):
                    transitions["g"][i] = future_ag
            transitions["r"][i] = self.reward_func(
                episode_batch["ag"][episode_idx][t + 1], transitions["g"][i], None
            )

        new_transitions = {}
        for k, v in transitions.items():
            if isinstance(v[0], dict):
                sub_keys = v[0].keys()
                new_transitions[k] = {
                    sub_key: np.stack([v_[sub_key] for v_ in v]) for sub_key in sub_keys
                }
            else:
                new_transitions[k] = np.stack(v)

        return new_transitions

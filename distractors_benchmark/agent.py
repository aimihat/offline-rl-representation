# python3
# Copyright 2018 DeepMind Technologies Limited. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""DQN agent implementation."""

import copy
from typing import Optional

import reverb
import sonnet as snt
import tensorflow as tf
import trfl
from acme import datasets, specs
from acme.adders import reverb as adders
from acme.agents import agent
from acme.agents.tf import actors
from acme.tf import savers as tf2_savers
from acme.tf import utils as tf2_utils
from acme.tf.utils import add_batch_dim, zeros_like
from acme.utils import loggers
from networks import QNetworkWithEncoder


class DQNAgent(agent.Agent):
    """DQN agent with a DBC representation learning loss."""

    def __init__(
        self,
        learner,
        wandb,
        environment_spec: specs.EnvironmentSpec,
        encoder_type: str = "mlp",
        encoder_feature_dim: int = 6,
        projection_feature_dim: int = 6,
        batch_size: int = 256,
        prefetch_size: int = 4,
        target_update_period: int = 100,
        samples_per_insert: float = 32.0,
        min_replay_size: int = 1000,
        max_replay_size: int = 1000000,
        importance_sampling_exponent: float = 0.2,
        priority_exponent: float = 0.6,
        n_step: int = 5,
        epsilon: Optional[tf.Tensor] = None,
        learning_rate: float = 1e-3,
        discount: float = 0.99,
        logger: loggers.Logger = None,
        checkpoint: bool = True,
        checkpoint_subpath: str = "~/acme/",
        policy_network: Optional[snt.Module] = None,
        beta=None,
    ):
        """Initialize the agent.

        Args:
          learner: the DQN learner used.
          environment_spec: description of the actions, observations, etc.
          network: the online Q network (the one being optimized)
          batch_size: batch size for updates.
          prefetch_size: size to prefetch from replay.
          target_update_period: number of learner steps to perform before updating
            the target networks.
          samples_per_insert: number of samples to take from replay for every insert
            that is made.
          min_replay_size: minimum replay size before updating. This and all
            following arguments are related to dataset construction and will be
            ignored if a dataset argument is passed.
          max_replay_size: maximum replay size.
          importance_sampling_exponent: power to which importance weights are raised
            before normalizing.
          priority_exponent: exponent used in prioritized sampling.
          n_step: number of steps to squash into a single transition.
          epsilon: probability of taking a random action; ignored if a policy
            network is given.
          learning_rate: learning rate for the q-network update.
          discount: discount to use for TD updates.
          logger: logger object to be used by learner.
          checkpoint: boolean indicating whether to checkpoint the learner.
          checkpoint_subpath: directory for the checkpoint.
          policy_network: if given, this will be used as the policy network.
            Otherwise, an epsilon greedy policy using the online Q network will be
            created. Policy network is used in the actor to sample actions.
        """

        # Create a replay server to add data to. This uses no limiter behavior in
        # order to allow the Agent interface to handle it.
        replay_table = reverb.Table(
            name=adders.DEFAULT_PRIORITY_TABLE,
            sampler=reverb.selectors.Prioritized(priority_exponent),
            remover=reverb.selectors.Fifo(),
            max_size=max_replay_size,
            rate_limiter=reverb.rate_limiters.MinSize(1),
            signature=adders.NStepTransitionAdder.signature(environment_spec),
        )
        self._server = reverb.Server([replay_table], port=None)

        # The adder is used to insert observations into replay.
        address = f"localhost:{self._server.port}"
        adder = adders.NStepTransitionAdder(
            client=reverb.Client(address), n_step=n_step, discount=discount
        )

        # The dataset provides an interface to sample from replay.
        replay_client = reverb.TFClient(address)
        dataset = datasets.make_reverb_dataset(
            server_address=address, batch_size=batch_size, prefetch_size=prefetch_size
        )

        # Create a create a Q-network
        self.network = QNetworkWithEncoder(
            environment_spec.actions.num_values,
            environment_spec.observations.shape[0],
            encoder_type=encoder_type,
            encoder_feature_dim=encoder_feature_dim,
            projection_feature_dim=projection_feature_dim,
        )

        # Create epsilon greedy policy network by default.
        if policy_network is None:
            # Use constant 0.05 epsilon greedy policy by default.
            if epsilon is None:
                epsilon = tf.Variable(0.05, trainable=False)
            policy_network = snt.Sequential(
                [
                    self.network,
                    lambda q: trfl.epsilon_greedy(q, epsilon=epsilon).sample(),
                ]
            )
        # Ensure that we create the variables before proceeding (maybe not needed).
        tf2_utils.create_variables(self.network, [environment_spec.observations])
        tf2_utils.create_variables(
            self.network._decoder,
            encoding_spec(self.network, environment_spec.observations),
        )
        if encoder_type:
            tf2_utils.create_variables(
                self.network._projector,
                encoding_spec(self.network, environment_spec.observations),
            )

        # Create a target network.
        target_network = copy.deepcopy(self.network)

        # Create the actor which defines how we take actions.
        actor = actors.FeedForwardActor(policy_network, adder)

        # The learner updates the parameters (and initializes them).
        dqn_learner = learner(
            network=self.network,
            target_network=target_network,
            discount=discount,
            importance_sampling_exponent=importance_sampling_exponent,
            learning_rate=learning_rate,
            target_update_period=target_update_period,
            dataset=dataset,
            replay_client=replay_client,
            logger=logger,
            checkpoint=checkpoint,
            wandb=wandb,
            beta=beta,
        )

        if checkpoint:
            self._checkpointer = tf2_savers.Checkpointer(
                directory=checkpoint_subpath,
                objects_to_save=dqn_learner.state,
                subdirectory="dqn_learner",
                time_delta_minutes=60.0,
            )
        else:
            self._checkpointer = None

        super().__init__(
            actor=actor,
            learner=dqn_learner,
            min_observations=max(batch_size, min_replay_size),
            observations_per_step=float(batch_size) / samples_per_insert,
        )

    def update(self):
        super().update()
        if self._checkpointer is not None:
            self._checkpointer.save()


def encoding_spec(network, observation_spec):
    """Returns the Acme spec for the encoder output."""

    dummy_input = zeros_like(observation_spec)
    encoding = network.encode(add_batch_dim(dummy_input))

    return specs.Array(
        shape=encoding.shape, dtype=encoding.dtype.as_numpy_dtype(), name="Encoding"
    )

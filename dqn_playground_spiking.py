import torch
import torch.nn as nn
import torch.functional as F
import torch.optim as optim
import numpy as np
import random
from gym import wrappers
from bindsnet import *
from time import time
from collections import deque, namedtuple
import itertools
import argparse
import pickle

parser = argparse.ArgumentParser()
parser.add_argument('--seed', type=int, default=0)
parser.add_argument('--dt', type=float, default=1.0)
parser.add_argument('--runtime', type=int, default=500)
parser.add_argument('--render_interval', type=int, default=None)
parser.add_argument('--plot_interval', type=int, default=None)
parser.add_argument('--plot', dest='plot', action='store_true')
parser.add_argument('--print_interval', type=int, default=None)
parser.add_argument('--gpu', dest='gpu', action='store_true')
parser.set_defaults(plot=False, render=False, gpu=False)
locals().update(vars(parser.parse_args()))

num_episodes = 100
action_pop_size = 1
hidden_neurons = 1000
readout_neurons= 4 * action_pop_size
epsilon = 0.0  #probability of picking random action
accumulator = False
probabilistic = False
noop_counter = 0


class Net(nn.Module):

    def __init__(self):
        super(Net, self).__init__()
        self.fc1 = nn.Linear(6400, 1000)
        self.fc2 = nn.Linear(1000, 4)


    def forward(self, x):
        x = F.relu(self.fc1(x))
        x = self.fc2(x)
        return x


# Atari Actions: 0 (noop), 1 (fire), 2 (right) and 3 (left) are valid actions
VALID_ACTIONS = [0, 1, 2, 3]
total_actions = len(VALID_ACTIONS)

# device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

if torch.cuda.is_available():
    torch.set_default_tensor_type('torch.cuda.FloatTensor')
    torch.cuda.manual_seed_all(seed)
    dtype = torch.cuda.FloatTensor
else:
    dtype = torch.FloatTensor

# Build network.

dqn_network = torch.load('dqn_time_difference_grayscale.pt')

for i in range(1, 11):
    print("starting for " + str(i*10) + "x weights")
    network = Network(dt=dt, accumulator=accumulator)

    # Layers of neurons.
    inpt = Input(n=6400, shape=[80, 80], traces=True)  # Input layer
    exc = AdaptiveLIFNodes(n=hidden_neurons, refrac=0, traces=True, thresh=-52, rest=-65.0, decay=1e-2, theta_plus= 0.05, theta_decay=1e-7, probabilistic=probabilistic)  # Excitatory layer
    readout = LIFNodes(n=4, refrac=0, traces=True, thresh=-52.0, rest=-65.0, decay=1e-2, probabilistic=probabilistic)  # Readout layer
    layers = {'X': inpt, 'E': exc, 'R': readout}

    # Connections between layers.
    # Input -> excitatory.
    input_exc_conn = Connection(source=layers['X'], target=layers['E'], w=torch.transpose(dqn_network.fc1.weight, 0, 1).view([80, 80, 1000])* i * 10)

    # Excitatory -> readout.
    exc_readout_conn = Connection(source=layers['E'], target=layers['R'], w=torch.transpose(dqn_network.fc2.weight, 0, 1).view([1000, 4]) * i * 10)

    # Add all layers and connections to the network.
    for layer in layers:
        network.add_layer(layers[layer], name=layer)


    # Load SpaceInvaders environment.
    environment = GymEnvironment('BreakoutDeterministic-v4')

    experiment_dir = os.path.abspath("./snn/{}".format(environment.env.spec.id))
    monitor_path = os.path.join(experiment_dir, "monitor")
    environment.env = wrappers.Monitor(environment.env, directory=monitor_path, resume=True)

    spikes = {}

    # Add all monitors to the network.
    for layer in set(network.layers):
        spikes[layer] = Monitor(network.layers[layer], state_vars=['s'], time=runtime)
        network.add_monitor(spikes[layer], name='%s_spikes' % layer)

    network.add_connection(input_exc_conn, source='X', target='E')
    network.add_connection(exc_readout_conn, source='E', target='R')

    # Voltage recording for excitatory and inhibitory layers.
    exc_voltage_monitor = Monitor(network.layers['E'], ['v'], time=runtime)
    readout_voltage_monitor = Monitor(network.layers['R'], ['v'], time=runtime)
    network.add_monitor(exc_voltage_monitor, name='exc_voltage')
    network.add_monitor(readout_voltage_monitor, name='readout_voltage')

    total_t = 0
    episode_rewards = np.zeros(num_episodes)
    q_spikes = []

    def policy(rspikes, eps):
        q_values = torch.Tensor([rspikes[(i * action_pop_size):(i * action_pop_size) + action_pop_size].sum()
                                   for i in range(total_actions)])
        A = np.ones(4, dtype=float) * eps / 4
        if torch.max(q_values) == 0:
            return [0.25, 0.25, 0.25, 0.25]
        best_action = torch.argmax(q_values)
        A[best_action] += (1.0 - eps)
        return A

    # Get voltage recording.
    exc_voltages = exc_voltage_monitor.get('v')
    readout_voltages = readout_voltage_monitor.get('v')

    obs = environment.reset()
    state = obs

    if plot:
        voltages = {'E': exc_voltages, 'R': readout_voltages}
        inpt = bernoulli(state, runtime).view(runtime, 6400).sum(0).view(80, 80)
        spike_ims, spike_axes = plot_spikes({layer: spikes[layer].get('s') for layer in spikes})
        inpt_axes, inpt_ims = plot_input(state, inpt)
        voltage_ims, voltage_axes = plot_voltages(voltages)
        plt.pause(1e-8)

    startTime = time()

    for i_episode in range(num_episodes):
        obs = environment.reset()
        state = torch.stack([obs] * 4, dim=2)

        for t in itertools.count():
            print("\rStep {} ({}) @ Episode {}/{}".format(
                t, total_t, i_episode + 1, num_episodes), end="")
            sys.stdout.flush()
            encoded_state = torch.tensor([0.25, 0.5, 0.75, 1]) * state.cuda()
            encoded_state = bernoulli(torch.sum(encoded_state, dim=2), runtime)
            inpts = {'X': encoded_state}
            hidden_spikes, readout_spikes = network.run(inpts=inpts, time=runtime)
            action_probs = policy(torch.sum(readout_spikes, dim=0), epsilon)
            action = np.random.choice(np.arange(len(action_probs)), p=action_probs)
            if action == 0:
                noop_counter += 1
            else:
                noop_counter = 0
            if noop_counter >= 20:
                action = np.random.choice(np.arange(len(action_probs)))
                noop_counter = 0
            next_obs, reward, done, _ = environment.step(VALID_ACTIONS[action])
            next_state = torch.clamp(next_obs - obs, min=0)
            next_state = torch.cat((state[:, :, 1:], next_state.view([next_state.shape[0], next_state.shape[1], 1])), dim=2)
            episode_rewards[i_episode] += reward
            q_spikes.append(torch.sum(readout_spikes, dim=0))
            total_t += 1

            if plot:
                # Get voltage recording.
                exc_voltages = exc_voltage_monitor.get('v')
                readout_voltages = readout_voltage_monitor.get('v')
                voltages = {'E': exc_voltages, 'R': readout_voltages}
                inpt = encoded_state.view(runtime, 6400).sum(0).view(80, 80)
                spike_ims, spike_axes = plot_spikes({layer: spikes[layer].get('s') for layer in spikes}, ims=spike_ims,
                                                    axes=spike_axes)
                inpt_axes, inpt_ims = plot_input(state, inpt, axes=inpt_axes, ims=inpt_ims)
                voltage_ims, voltage_axes = plot_voltages(voltages, ims=voltage_ims, axes=voltage_axes)
                plt.pause(1e-8)

            if done:
                print("\nEpisode Reward: {}".format(episode_rewards[i_episode]))
                break

            state = next_state
            obs = next_obs

        # np.savetxt('analysis/rewards_snn_prob_tdg_10_100x.txt', episode_rewards)
        # pickle.dump(q_values, open("analysis/q_vals_snn_prob_tdg_10_100x.txt", "wb"))

    endTime = time()

    print("\nTotal time taken:", endTime - startTime)
    np.savetxt('analysis/rewards_snn_tdg_'+ str(i*10) +'x.txt', episode_rewards)
    pickle.dump(q_spikes, open("analysis/q_vals_snn_tdg_"+ str(i*10) +"x.txt", "wb"))




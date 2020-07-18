import re

try:
    from graphviz import Digraph
except ModuleNotFoundError:
    # TODO: better handling
    print("Install `graphviz` for correct use")


def get_object_id(obj) -> str:
    """
    Returns the identity of the object as str
    """
    return str(id(obj))


def get_conditions_id(obj) -> str:
    """
    Returns the identity of class Conditions as str
    """
    return '{}|conditions'.format(id(obj))


def get_readable_process_name(process) -> str:
    """
    Returns readable process name
    """
    process_name = process.process_name
    if process_name == 'process':
        process_name = process.__name__

    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1 \2', str(process_name))
    return re.sub('([a-z0-9])([A-Z])', r'\1 \2', s1)


def get_target_states(process) -> set:
    """
    Returns a set of target states of provided transitions under the process,
    including 'in progress' and 'failed' states
    """
    states = set()
    for transition in process.transitions:
        states.add(transition.target)
        if transition.failed_state:
            states.add(transition.failed_state)
        if transition.in_progress_state:
            states.add(transition.in_progress_state)
    return states


def get_all_target_states(process) -> set:
    """
    Returns a set of all target states of provided process class,
    including target states of nested processes.
    """
    states = get_target_states(process)
    for sub_process in process.nested_processes:
        states |= get_all_target_states(sub_process)
    return states


def get_all_states(process) -> set:
    """
    Returns a set of all states available under provided process, including nested process
    """
    states = set()
    for transition in process.transitions:
        states.add(transition.target)
        if transition.in_progress_state:
            states.add(transition.in_progress_state)
        if transition.failed_state:
            states.add(transition.failed_state)
        states |= set(transition.sources)

    for sub_process in process.nested_processes:
        states |= get_all_states(sub_process)

    return states


def annotate_nodes(process):
    """
      This function annotate node names and nodes into two dicts:
      - node_names contains either process or transition unique node name.
      - nodes contain the information of the given process
      :param process: Process class
      It should return a directed graph where every node has a unique name,
      nodes could be connected by arrows.

      It assigns a state to a node only and only if the state hasn't been used before in the graph.
      In other words, a state is always on an upper level before it's used. It helps to display sub-graphs
      around that state, which provides a better understanding of how the process structured.

      Supported the following types of nodes:
      - process
      - process_conditions
      - transition
      - transition_conditions
      - state

      and list of paths - from and to

      parameters:
      - node_name - unique node name. It needs to know the exact node path,
          as there is no unique identifier between transitions, processes, and other nodes.
          So, the node name is combined through the graph path.

      - name - displayed name
      - type - could be: process, process_conditions, transition, transition_conditions

      # Example:
      nodes = {
          'name': 'Main process',
          'type': 'process',
          'nodes': [
              {
                  'name': 'my unique name of the node',
                  'nodes': [
                      {
                          'type': 'transition',
                          'name': 'my transition'
                      },
                      {
                          'type': 'condition',
                          'name': 'my condition'
                      }
                  ],
                  'type': 'process'
              },
              {
                  'name': 'my state',
                  'type': 'state',
              }
          ]
      }
      """
    used_states = set()

    def annotate_sub_process_nodes(sub_process, node_name):
        node_name += get_readable_process_name(sub_process) + '|'
        # process
        node = {
            'id': get_object_id(sub_process),
            'name': get_readable_process_name(sub_process),
            'type': 'process',
            'nodes': []
        }
        # process permissions as conditions
        if sub_process.permissions:
            node['nodes'].append({
                'id': get_conditions_id(sub_process),
                'name': '\n'.join([permission.__name__ for permission in sub_process.permissions]),
                'type': 'process_conditions',
            })
        # process conditions
        if sub_process.conditions:
            node['nodes'].append({
                'id': get_conditions_id(sub_process),
                'name': '\n'.join([condition.__name__ for condition in sub_process.conditions]),
                'type': 'process_conditions',
            })

        # transitions
        for transition in sub_process.transitions:
            node['nodes'].append({
                'id': get_object_id(transition),
                'name': transition.action_name,
                'type': 'transition',
            })
            # transition permissions as conditions
            if transition.permissions.commands:
                node['nodes'].append({
                    'id': get_conditions_id(transition),
                    'name': '\n'.join([permission.__name__ for permission in transition.permissions.commands]),
                    'type': 'transition_conditions',
                })
            # transition conditions
            if transition.conditions.commands:
                node['nodes'].append({
                    'id': get_conditions_id(transition),
                    'name': '\n'.join([condition.__name__ for condition in transition.conditions.commands]),
                    'type': 'transition_conditions',
                })

        # it finds all intersections between the current process' states and its sub process' states
        states = get_target_states(sub_process)
        for sub_process1 in sub_process.nested_processes:
            for sub_process2 in sub_process.nested_processes:
                if sub_process1 != sub_process2:
                    states |= (get_all_target_states(sub_process1) &
                               get_all_target_states(sub_process2))

        # it assigns all intersect states to this particular process level for better visibility,
        # as the states that deeper than this process should not intersect with each other.
        for state_name in states - used_states:
            node['nodes'].append({
                'id': state_name,
                'name': state_name,
                'type': 'state',
            })
            used_states.add(state_name)

        for sub_process in sub_process.nested_processes:
            node['nodes'].append(annotate_sub_process_nodes(sub_process, node_name))
        return node

    main_node = annotate_sub_process_nodes(process, node_name='')
    for state in get_all_states(process) - used_states:
        main_node['nodes'].append({
            'id': state,
            'name': state,
            'type': 'state',
        })

    return main_node


def fsm_paths(process, state):
    paths = set()
    visited_state = []

    def add_path(target, source):
        """
        the path given from the target to the source, but it should swap when added
        """
        paths.add((source, target))

    def get_available_transitions(process_class, state):
        for transition in process_class.transitions:
            if state in transition.sources:
                yield transition
        for sub_process in process_class.nested_processes:
            for transition in get_available_transitions(sub_process, state):
                yield transition

    def dfs(current_state):
        """
        This function goes from "the bottom to the top" through the available transitions and
         consolidate the path from transition to the state,
         including transition.

        target state <- transition <- transition conditions <- current state

        Where the path is a tuple of unique id.
        For example:
        [
            ('target state', '12312312'),  # target state <- transition
            ('12312312', '12312312|conditions'),  # transition <- transition conditions
            ('12312312|conditions', 'current state'),  #  transition conditions <- current state
        ]
        :param current_state:
        :return:
        """
        visited_state.append(current_state)

        for transition in get_available_transitions(process, current_state):
            current = get_object_id(transition)
            add_path(transition.target, current)

            if transition.conditions.commands:
                target = get_conditions_id(transition)
                add_path(current, target)
                current = target

            add_path(current, current_state)

            if transition.target not in visited_state:
                dfs(transition.target)

    dfs(state)
    return paths


def get_graph_from_node(main_node, paths, skip_main_process=False):
    def draw_node(node, graph):
        if node['type'] == 'process':
            graph.attr(label=node['name'])
            return

        if node['type'] == 'process_conditions':
            graph.attr('node', style='filled', fillcolor='white', shape='diamond')

        if node['type'] == 'transition':
            graph.attr('node', style='filled', fillcolor='lightgrey', shape='record')

        if node['type'] == 'transition_conditions':
            graph.attr('node', style='filled', fillcolor='lightgrey', shape='diamond')

        if node['type'] == 'state':
            graph.attr('node', style='filled', fillcolor='white', shape='oval')

        graph.node(name=node['id'], label=node['name'])

    def draw_process(node, graph):
        """
        :param node: node is process
        :param graph:
        :return:
        """
        for node in node['nodes']:
            if node['type'] == 'process' and node['nodes']:
                # for every process we should draw a new cluster
                with graph.subgraph(name="cluster_{}".format(node['id'])) as subgraph:
                    draw_process(node, subgraph)
                    draw_node(node, subgraph)
            else:
                draw_node(node, graph)

    def draw_edges(graph):
        for from_node, to_node in paths:
            graph.edge(from_node, to_node)

    engine = 'fdp'
    digraph = Digraph(main_node['name'], filename=main_node['name'],
                      engine=engine, node_attr={'shape': 'record'})

    if not skip_main_process:
        main_node = dict(nodes=[main_node], type='process')
    draw_process(main_node, digraph)
    draw_edges(digraph)

    digraph.attr(overlap='false')
    return digraph


def get_graph_from_process(process_class, state, skip_main_process=False):
    node = annotate_nodes(process_class)
    paths = fsm_paths(process_class, state)
    graph = get_graph_from_node(node, paths, skip_main_process)
    return graph


def display_process(process_class, state, skip_main_process=False):
    graph = get_graph_from_process(process_class, state, skip_main_process)
    try:
        graph.view()
    except Exception as ex:
        if hasattr(ex, 'stderr'):
            print(ex.stderr)
        else:
            print(ex)

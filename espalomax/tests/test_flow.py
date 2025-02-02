def test_janossy_parameter_convert():
    import espalomax as esp
    polynomial_parameters = esp.flow.get_polynomial_parameters()


def test_poly_eval():
    import jax
    import espalomax as esp
    polynomial_parameters = esp.flow.get_polynomial_parameters()

    from functools import partial
    from openff.toolkit.topology import Molecule
    molecule = Molecule.from_smiles("CC")

    graph = esp.Graph.from_openff_molecule(molecule)
    model = esp.nn.Parametrization(
        representation=esp.nn.GraphSageModel(8, 3),
        janossy_pooling=esp.nn.JanossyPooling(8, 3, out_features=polynomial_parameters),

    )
    nn_params = model.init(jax.random.PRNGKey(2666), graph)
    flow_params = model.apply(nn_params, graph)
    ff_params = esp.flow.eval_polynomial(0.5, flow_params)

def test_forward_training():
    import jax
    import jax.numpy as jnp
    from jax.experimental.ode import odeint
    import espalomax as esp
    polynomial_parameters = esp.flow.get_polynomial_parameters()

    from functools import partial
    from openff.toolkit.topology import Molecule
    molecule = Molecule.from_smiles("CC")
    graph = esp.Graph.from_openff_molecule(molecule)
    model = esp.nn.Parametrization(
        representation=esp.nn.GraphSageModel(8, 3),
        janossy_pooling=esp.nn.JanossyPooling(8, 3, out_features=polynomial_parameters),

    )
    nn_params = model.init(jax.random.PRNGKey(2666), graph)

    parameters  = esp.graph.parameters_from_molecule(molecule)

    from jax_md import space
    displacement_fn, shift_fn = space.free()

    from jax_md.mm import mm_energy_fn
    energy_fn, _ = mm_energy_fn(
        displacement_fn, default_mm_parameters=parameters,
    )

    from diffrax import diffeqsolve, ODETerm, Dopri5

    def f(t, x, flow_params):
        ff_params = esp.flow.eval_polynomial(t, flow_params)
        f_esp = jax.vmap(jax.grad(esp.mm.get_energy, 1), (None, 0))(ff_params, x)
        return f_esp

    term = ODETerm(f)
    solver = Dopri5()

    def loss(nn_params, x):
        flow_params = model.apply(nn_params, graph)
        flow_params = esp.flow.constraint_polynomial_parameters(flow_params)
        y = diffeqsolve(term, solver, args=flow_params, t0=0.0, t1=1.0,  dt0=0.1, y0=x, max_steps=1000).ys[-1]
        u = jax.vmap(energy_fn)(y).mean()
        jax.debug.print("{x}", x=u)
        return u

    @jax.jit
    def step(state, x):
        grads = jax.grad(loss)(state.params, x)
        state = state.apply_gradients(grads=grads)
        return state


    import optax
    tx = optax.adamw(1e-5)
    from flax.training.train_state import TrainState
    state = TrainState.create(apply_fn=model.apply, params=nn_params, tx=tx)
    key = jax.random.PRNGKey(2666)
    for _ in range(10000):
        this_key, key = jax.random.split(key)
        x = jax.random.normal(key, shape=(10, 8, 3))
        state = step(state, x)


def test_backward_training():
    import jax
    import jax.numpy as jnp
    from jax.experimental.ode import odeint
    import espalomax as esp
    polynomial_parameters = esp.flow.get_polynomial_parameters()

    from functools import partial
    from openff.toolkit.topology import Molecule
    molecule = Molecule.from_smiles("CC")
    graph = esp.Graph.from_openff_molecule(molecule)
    model = esp.nn.Parametrization(
        representation=esp.nn.GraphSageModel(8, 3),
        janossy_pooling=esp.nn.JanossyPooling(8, 3, out_features=polynomial_parameters),

    )
    nn_params = model.init(jax.random.PRNGKey(2666), graph)
    molecule.generate_conformers()
    coordinate = molecule.conformers[0]._value * 0.1
    coordinate = jnp.array(coordinate)
    parameters  = esp.graph.parameters_from_molecule(molecule)

    from jax_md import space
    displacement_fn, shift_fn = space.free()

    from jax_md.mm import mm_energy_fn
    energy_fn, _ = mm_energy_fn(
        displacement_fn, default_mm_parameters=parameters,
    )

    from jax_md import simulate
    temperature = 1.0
    dt = 1e-3
    init, update = simulate.nvt_nose_hoover(energy_fn, shift_fn, dt, temperature)
    md_state = init(jax.random.PRNGKey(2666), coordinate)
    update = jax.jit(update)

    def f(x, t, flow_params):
        ff_params = esp.flow.eval_polynomial(t, flow_params)
        f_esp = jax.vmap(jax.grad(esp.mm.get_energy, 1), (None, 0))(ff_params, x)
        return f_esp

    def dynamics_and_trace(x, t, flow_params, key):
        dynamics = partial(f, flow_params=flow_params)
        trace = partial(esp.flow.get_trace, fn=dynamics, key=key)
        def fn(state, t):
            x, _trace = state
            return dynamics(x, t), trace(x, t, key)
        return fn

    def loss(nn_params, x, key):
        flow_params = model.apply(nn_params, graph)
        flow_params = esp.flow.constraint_polynomial_parameters(flow_params)
        fn = partial(dynamics_and_trace, flow_params=flow_params, key=key)
        trace0 = jnp.zeros(shape=x.shape[:-2])
        y, logdet = odeint(fn, (x, trace0), jnp.array([0.0, 1.0]), flow_params, rtol=0.01, atol=0.01)
        y, logdet = y[-1], logdet[-1]
        loss = (y ** 2).mean()
        jax.debug.print("{x}", x=loss)
        return loss


    def step(state, x):
        loss(state.params, x)
        grads = jax.grad(loss)(state.params, x)
        state = state.apply_gradients(grads=grads)
        return state


    @jax.jit
    def simulation_and_step(md_state, nn_state):
        traj = []
        for _ in range(10):
            md_state = update(md_state)
            traj.append(md_state.position)
        traj = jnp.stack(traj).astype(jnp.float32)
        nn_state = step(nn_state, traj)
        return md_state, nn_state

    import optax
    tx = optax.adamw(1e-5)
    from flax.training.train_state import TrainState
    nn_state = TrainState.create(apply_fn=model.apply, params=nn_params, tx=tx)
    key = jax.random.PRNGKey(2666)
    for _ in range(1000000):
        this_key, key = jax.random.split(key)
        md_state, nn_state = simulation_and_step(md_state, nn_state)


test_backward_training()

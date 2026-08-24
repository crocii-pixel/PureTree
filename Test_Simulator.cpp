#include <iostream>
#include <vector>
#include <string>
#include <cassert>
#include <iomanip>
#include "CPureTree.h"
#include "Simulator.h"
#include "TypeRegistry.h"

/**
 * @brief Test suite for CPureTree core functionality.
 */
void run_core_tree_tests() {
    std::cout << "[Test 1] Core Tree Operations" << std::endl;

    nsTree::TreeConfig config;
    config.enforceUniqueKeys = true;
    config.allowEmptyKeys = false;
    nsTree::CPureTree tree(config);

    // 1. Node Creation
    auto h1 = tree.addNode(1, "Node_A", 100);
    auto h2 = tree.addNode(1, "Node_B", 200);
    assert(h1 != 0 && h2 != 0);

    // 2. Unique Key Enforcement
    auto h3 = tree.addNode(1, "Node_A", 300); 
    assert(h3 == 0); // Should fail due to duplicate key

    // 3. Link Creation
    auto l1 = tree.addLink(h1, h2, 10, {1.0f, 1.0f, 1.0f});
    assert(l1 != 0);
    assert(tree.getNode(h1)->outs.size() == 1);
    assert(tree.getNode(h2)->ins.size() == 1);

    // 4. Search
    assert(tree.findNode("Node_A") == h1);
    assert(tree.findNode("Node_NonExistent") == 0);

    std::cout << "  -> Core Tree Operations: PASSED" << std::endl;
}

/**
 * @brief Test suite for the new Linearization and FilterMode logic.
 */
void run_filter_logic_tests() {
    std::cout << "\n[Test 2] Linearization and FilterMode" << std::endl;

    nsTree::CPureTree tree; // Default: UniqueKeys=true, allowEmpty=false
    auto root = tree.addNode(0, "Root");
    auto child1 = tree.addNode(0, "Child_Type1");
    auto child2 = tree.addNode(0, "Child_Type2");

    int type101 = nsTree::TypeRegistry::instance().resolve("LinkType101");
    int type102 = nsTree::TypeRegistry::instance().resolve("LinkType102");

    tree.addLink(root, child1, type101); // Link Type 101
    tree.addLink(root, child2, type102); // Link Type 102

    // 1. FilterMode::Only
    std::vector<bool> filterMask101 = nsTree::TypeRegistry::instance().createFilterMask({type101});
    std::vector<nsTree::hTree> onlyType101;
    tree.serialize(root, onlyType101, true, nsTree::FilterMode::Only, filterMask101);
    // Expected: Root, Child1 (Child2 skipped because link type 102 is not in 'Only')
    assert(onlyType101.size() == 2);

    // 2. FilterMode::Skip
    std::vector<nsTree::hTree> skipType101;
    tree.serialize(root, skipType101, true, nsTree::FilterMode::Skip, filterMask101);
    // Expected: Root, Child2 (Child1 skipped)
    assert(skipType101.size() == 2);

    // 3. Early Exit (Only with empty filter)
    std::vector<nsTree::hTree> earlyExit;
    std::vector<bool> emptyMask;
    tree.serialize(root, earlyExit, true, nsTree::FilterMode::Only, emptyMask);
    assert(earlyExit.size() == 1 && earlyExit[0] == root);

    std::cout << "  -> Linearization and FilterMode: PASSED" << std::endl;
}

/**
 * @brief Complex simulation test representing a software system impact analysis.
 */
void run_simulation_impact_test() {
    std::cout << "\n[Test 3] System Impact Simulation" << std::endl;

    nsTree::TreeConfig config;
    config.enforceUniqueKeys = true;
    nsTree::CPureTree tree(config);

    // System Components
    auto configNode = tree.addNode(1, "Config_Service");
    auto authNode   = tree.addNode(1, "Auth_Service");
    auto dbNode     = tree.addNode(1, "Database");
    auto uiNode     = tree.addNode(1, "User_Interface");

    // Relationships
    // Config -> Auth (High impact: force 0.9)
    tree.addLink(configNode, authNode, 100, {0.9f, 1.0f, 1.0f});
    // Auth -> DB (Medium impact: force 0.7)
    tree.addLink(authNode, dbNode, 100, {0.7f, 1.0f, 1.0f});
    // DB -> UI (Low impact/Long distance: dist 5.0)
    tree.addLink(dbNode, uiNode, 100, {0.5f, 5.0f, 1.0f});

    std::cout << "  Injected Shock: 3.0 units into 'Config_Service'" << std::endl;

    // Run Simulation
    auto results = nsSim::Simulator::run(tree, configNode, 3.0f, nsSim::Rules::Linear);

    // Verification
    assert(results[configNode].activated == true);
    assert(results[authNode].activated == true); // 3.0 * 0.9 / 1.0 = 2.7 > 1.0
    
    // Auth energy (2.7) -> DB: 2.7 * 0.7 / 1.0 = 1.89 > 1.0
    assert(results[dbNode].activated == true);

    // DB energy (1.89) -> UI: 1.89 * 0.5 / 5.0 = 0.189 < 1.0
    assert(results[uiNode].activated == false);

    std::cout << "  Final Analysis:" << std::endl;
    for (auto const& [id, state] : results) {
        std::cout << "    Node: " << std::left << std::setw(15) << tree.getNode(id)->key 
                  << " | Energy: " << std::fixed << std::setprecision(2) << state.energy 
                  << " | Triggered: " << (state.activated ? "YES" : "NO") << std::endl;
    }

    std::cout << "  -> System Impact Simulation: PASSED" << std::endl;
}

int main() {
    std::cout << "===========================================" << std::endl;
    std::cout << "   PureTree & Simulator Integrated Tests   " << std::endl;
    std::cout << "===========================================" << std::endl;

    try {
        run_core_tree_tests();
        run_filter_logic_tests();
        run_simulation_impact_test();

        std::cout << "\n===========================================" << std::endl;
        std::cout << "       ALL TESTS COMPLETED SUCCESSFULLY    " << std::endl;
        std::cout << "===========================================" << std::endl;
    } catch (const std::exception& e) {
        std::cerr << "\n[ERROR] Test Suite Failed: " << e.what() << std::endl;
        return 1;
    }

    return 0;
}
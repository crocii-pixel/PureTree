#include <iostream>
#include <string>
#include <vector>
#include <thread>
#include <atomic>
#include <cassert>
#include <chrono>
#include <random>

#include "Keys.h"
#include "Account.h"

// Complex data structure for testing
struct ComplexData {
    int id;
    std::string message;
    std::vector<double> values;
};

void run_harsh_test() {
    std::cout << "--- Starting Harsh Scenario Test ---" << std::endl;

    // 1. Re-entrancy and Chain Reaction Test
    {
        std::cout << "[Test 1] Re-entrancy and Chain Reaction Test..." << std::endl;
        Account boss(new int(1));
        int trigger_count = 0;

        // "EventA" triggers "EventB", and "EventB" triggers "EventA" back (up to 10)
        keys().addEvent(boss, "EventA", [&](const std::string& k, const AnyValue& v) {
            int current = v.to<int>();
            if (current < 10) {
                std::cout << "  EventA -> Raising EventB (" << current << ")" << std::endl;
                key("EventB", current + 1);
            }
            trigger_count++;
        });

        keys().addEvent(boss, "EventB", [&](const std::string& k, const AnyValue& v) {
            int current = v.to<int>();
            std::cout << "  EventB -> Raising EventA (" << current << ")" << std::endl;
            key("EventA", current);
            trigger_count++;
        });

        key("EventA", 1);
        
        // Expected total triggers: 19 (A(1)->B(2)->A(2)->B(3)...)
        std::cout << "  Total trigger count: " << trigger_count << std::endl;
        assert(trigger_count == 19);
        std::cout << "  [PASS] Re-entrancy verified." << std::endl;
    }

    // 2. Large-scale Zombie Defense and ABA Test
    {
        std::cout << "[Test 2] Large-scale Zombie Defense and ABA Test..." << std::endl;
        const int TOTAL_SUBS = 50000;
        std::vector<std::unique_ptr<Account>> followers;
        std::atomic<int> active_receivers{0};

        // Create 50,000 subscriptions
        for (int i = 0; i < TOTAL_SUBS; ++i) {
            auto acc = std::make_unique<Account>(new int(i));
            keys().addEvent(*acc, "StressKey", [&](const std::string&, const AnyValue&) {
                active_receivers++;
            });
            followers.push_back(std::move(acc));
        }

        // Randomly remove half (to create "Zombie" handle scenarios)
        std::random_device rd;
        std::mt19937 g(rd());
        std::shuffle(followers.begin(), followers.end(), g);
        followers.erase(followers.begin(), followers.begin() + (TOTAL_SUBS / 2));

        // Trigger event
        active_receivers = 0;
        key("StressKey", AnyValue(std::string("Survive!")));

        std::cout << "  Surviving subscribers: " << followers.size() 
                  << " | Actual received: " << active_receivers.load() << std::endl;
        assert(active_receivers.load() == static_cast<int>(followers.size()));
        std::cout << "  [PASS] Zombie blocking and ABA defense verified." << std::endl;
    }

    // 3. Multi-threaded Race Condition Test (Deadlock check)
    {
        std::cout << "[Test 3] Multi-threaded Race Condition Test..." << std::endl;
        std::atomic<bool> running{true};
        std::atomic<int> event_count{0};

        // Thread A: Raising events continuously
        std::thread publisher([&]() {
            while (running) {
                key("GlobalKey", event_count.fetch_add(1));
                std::this_thread::yield();
            }
        });

        // Thread B: Subscribing and unsubscribing continuously
        std::thread subscriber([&]() {
            while (running) {
                {
                    Account temp_acc(new int(99));
                    keys().addEvent(temp_acc, "GlobalKey", [](const std::string&, const AnyValue&) {});
                    std::this_thread::sleep_for(std::chrono::microseconds(10));
                } // Auto-unsubscribe on destruction
            }
        });

        std::this_thread::sleep_for(std::chrono::seconds(2));
        running = false;
        publisher.join();
        subscriber.join();
        std::cout << "  Total events published over 2 seconds: " << event_count.load() << std::endl;
        std::cout << "  [PASS] No crashes or deadlocks in multi-threaded environment." << std::endl;
    }

    // 4. Complex Type Data Integrity Test
    {
        std::cout << "[Test 4] Complex Type Data Integrity Test..." << std::endl;
        Account inspector(new int(7));
        ComplexData sent_data = { 42, "Deep Learning", {1.1, 2.2, 3.3} };
        bool success = false;

        keys().addEvent(inspector, "DataKey", [&](const std::string&, const AnyValue& v) {
            try {
                auto received = v.to<ComplexData>();
                if (received.id == 42 && received.message == "Deep Learning" && received.values.size() == 3) {
                    success = true;
                }
            } catch (...) {
                success = false;
            }
        });

        key("DataKey", sent_data);
        assert(success);
        std::cout << "  [PASS] Complex object (with vector) delivered successfully." << std::endl;
    }

    std::cout << "--- All Harsh Tests Passed! System is highly robust. ---" << std::endl;
}

int main() {
    // Disable synchronization with stdio for performance and use standard locale
    std::ios_base::sync_with_stdio(false);

    try {
        run_harsh_test();
    } catch (const std::exception& e) {
        std::cerr << "Exception occurred during test: " << e.what() << std::endl;
        return 1;
    }
    return 0;
}